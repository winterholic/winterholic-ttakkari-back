"""GoogleAccountService: Google 계정 단위 로그인·세션·재인증 상태의 단일 진입점(ADR 0051).

계정 수준(이 서비스가 소유)
- 로그인 창 수명주기(login.manager)와 세션 파일(session). 같은 계정의 세션은 등록된 모든 기능(registry)이 공유한다.
- 상태: login_required → session_saved → ready ⇄ reauth_required. 세션 만료(reauth)는 어느 기능이 감지하든 계정 상태로 올라가고,
  알림은 계정당 한 번만 보낸다(DB 에서 발송권을 먼저 잡은 요청만 보낸다). 다시 로그인하면 풀린다.
- 접속 확인(verify)이 성공한 기능이 있으면 ready. 이 서비스는 Google 에 요청하지 않는다(상태 조회는 브라우저·네트워크를 쓰지 않는다).

기능 수준(각 기능이 소유, 여기서는 읽기만)
- 쿨다운(429)·보호 신호(blocked)·기능별 접속 확인 이력. 기능의 describe() 요약만 상태 화면에 싣는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.google_account import errors, login, metrics, redact, registry, session
from ttakkari.models import GoogleAccount

log = logging.getLogger(__name__)

DEFAULT_ACCOUNT = session.DEFAULT_ACCOUNT
ACCOUNT_PAGE = "/settings#google-accounts"  # 재로그인 알림이 여는 화면


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class GoogleAccountService:
    # ------------------------------------------------------------------ 상태

    async def _row(self, db, account_id: str) -> GoogleAccount:
        session.check_account(account_id)
        row = await db.get(GoogleAccount, account_id)
        if row is None:
            await db.execute(insert(GoogleAccount).values(
                account_id=account_id, state="login_required", created_at=_now(), updated_at=_now()
            ).on_conflict_do_nothing())
            await db.commit()
            row = await db.get(GoogleAccount, account_id)
        assert row is not None
        return row

    async def _set_state(self, account_id: str, state: str, message: str | None = None, **fields: Any) -> None:
        async with sessionmaker()() as db:
            row = await self._row(db, account_id)
            row.state, row.message = state, message
            for k, v in fields.items():
                setattr(row, k, v)
            await db.commit()

    @staticmethod
    def state_of(row: GoogleAccount | None, account_id: str) -> tuple[str, str | None]:
        """저장된 상태 + 세션 파일을 합친 현재 계정 상태. 기능별 쿨다운·blocked 는 여기에 없다."""
        if not get_settings().google_accounts_enabled:
            return "disabled", "Google 계정 연결이 꺼져 있습니다."
        if not session.exists(account_id):
            return "login_required", "로그인한 적이 없습니다. 로그인 창을 열어 한 번 로그인해 주세요."
        if row is not None and row.state == "reauth_required":
            return "reauth_required", row.message
        if row is not None and row.state == "ready":
            return "ready", None
        return "session_saved", "세션은 저장됐지만 아직 접속을 확인하지 않았습니다."

    async def effective(self, account_id: str) -> tuple[str, str | None]:
        """기능이 요청 전에 보는 계정 상태. disabled | login_required | reauth_required | session_saved | ready."""
        session.check_account(account_id)
        async with sessionmaker()() as db:
            return self.state_of(await db.get(GoogleAccount, account_id), account_id)

    async def account_ids(self) -> list[str]:
        async with sessionmaker()() as db:
            rows = list(await db.scalars(select(GoogleAccount.account_id)))
        ids = {DEFAULT_ACCOUNT, *rows, *session.known_account_ids()}
        return sorted(ids, key=lambda i: (i != DEFAULT_ACCOUNT, i))

    async def _features(self, account_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in registry.consumers():
            summary: dict[str, Any] = {}
            if c.describe is not None:
                try:
                    summary = await c.describe(account_id)
                except Exception as e:  # noqa: BLE001 -- 한 기능의 상태 조회 실패가 계정 화면 전체를 막지 않게
                    log.warning("google account feature describe failed: %s %s", c.id, redact.safe_error(e))
                    summary = {"state": "unknown", "message": "기능 상태를 읽지 못했습니다."}
            out.append({
                "id": c.id,
                "label": c.label,
                "description": c.description,
                "path": c.path,
                "enabled": c.enabled(),
                "covered": session.covers(account_id, registry.BASE_COOKIE_SUFFIXES + c.cookie_domains)
                if session.exists(account_id) else None,
                "can_verify": c.verify is not None,
                "can_resume": c.resume is not None,
                **summary,
            })
        return out

    async def status(self, account_id: str = DEFAULT_ACCOUNT) -> dict[str, Any]:
        """마지막으로 알려진 상태만 본다. 브라우저를 띄우거나 Google 에 요청하지 않는다."""
        session.check_account(account_id)
        async with sessionmaker()() as db:
            row = await db.get(GoogleAccount, account_id)
            state, message = self.state_of(row, account_id)
        stored = session.exists(account_id)
        return {
            "account_id": account_id,
            "label": row.label if row is not None else None,
            "state": state,
            "message": message,
            "logged_in": None if state in {"session_saved", "disabled"} else state == "ready",
            "reauth_required": state == "reauth_required",
            "last_login_at": _iso(row.last_login_at) if row else None,
            "last_ok_at": _iso(row.last_ok_at) if row else None,
            "last_checked_at": _iso(row.last_checked_at) if row else None,
            "last_error_code": row.last_error_code if row else None,
            "session": {
                "stored": stored,
                "cookie_names": session.cookie_names(account_id),  # 이름만. 값은 어디에도 없다.
                "permissions_ok": session.mode_ok(account_id) if stored else None,
                "encrypted": False,
                "protection": "소유자 전용 파일 권한(0600/0700)만 사용합니다. 암호화하지 않습니다.",
            },
            "login": login.manager.state(account_id).to_dict(),
            "browser_available": await asyncio.to_thread(login.find_chrome) is not None,
            "features": await self._features(account_id),
            "metrics": metrics.snapshot(),
        }

    async def list_accounts(self) -> list[dict[str, Any]]:
        return [await self.status(a) for a in await self.account_ids()]

    # ------------------------------------------------------------------ 로그인 수명주기

    async def start_login(self, account_id: str = DEFAULT_ACCOUNT, feature: str | None = None) -> dict[str, Any]:
        st = await login.manager.start(account_id, on_done=self._on_login_done, feature=feature)
        return st.to_dict()

    async def complete_login(self, account_id: str = DEFAULT_ACCOUNT) -> dict[str, Any]:
        return (await login.manager.complete(account_id)).to_dict()

    async def cancel_login(self, account_id: str = DEFAULT_ACCOUNT) -> dict[str, Any]:
        return (await login.manager.cancel(account_id)).to_dict()

    async def _on_login_done(self, st: login.LoginState) -> None:
        if st.state != "done":
            if not session.exists(st.account_id):
                await self._set_state(st.account_id, "login_required", st.message or "로그인이 완료되지 않았습니다.",
                                      reauth_notified_at=None)
            return
        await self._set_state(st.account_id, "session_saved", "로그인 세션을 저장했습니다. 접속을 확인하는 중입니다.",
                              reauth_notified_at=None, last_error_code=None, last_login_at=_now())
        # 기능마다 자기 캐시를 비우고(필요하면) 접속을 확인한다. 한 기능의 실패가 다른 기능을 막지 않는다.
        await registry.fire("on_session_saved", st.account_id)

    async def delete_session(self, account_id: str = DEFAULT_ACCOUNT) -> dict[str, Any]:
        """저장된 세션 파일을 지우고 로그인 필요 상태로 돌린다(Google 쪽 로그아웃은 아님)."""
        session.check_account(account_id)
        session.delete(account_id)
        await self._set_state(account_id, "login_required", "저장된 세션을 삭제했습니다.", reauth_notified_at=None)
        await registry.fire("on_session_removed", account_id)
        return await self.status(account_id)

    # ------------------------------------------------------------------ 기능이 부르는 상태 갱신

    async def mark_ok(self, account_id: str) -> None:
        """기능이 이 계정으로 Google 요청에 성공했다. 재인증 필요 상태는 덮어쓰지 않는다(느린 성공이 만료 기록을 지우지 않게)."""
        async with sessionmaker()() as db:
            await self._row(db, account_id)
            now = _now()
            await db.execute(update(GoogleAccount).where(
                GoogleAccount.account_id == account_id, GoogleAccount.state != "reauth_required",
            ).values(state="ready", message=None, last_error_code=None, reauth_notified_at=None,
                     last_ok_at=now, last_checked_at=now, updated_at=now))
            await db.commit()

    async def mark_reauth(self, account_id: str, message: str, *, feature: str | None = None) -> None:
        """세션 만료를 계정 상태로 올린다. 어느 기능이 감지했든 알림은 한 번만 보낸다."""
        async with sessionmaker()() as db:
            await self._row(db, account_id)
            now = _now()
            # 여러 요청이 동시에 만료를 감지해도 DB에서 한 요청만 알림 발송권을 얻는다.
            claimed = await db.execute(update(GoogleAccount).where(
                GoogleAccount.account_id == account_id,
                GoogleAccount.reauth_notified_at.is_(None),
            ).values(state="reauth_required", message=message, last_error_code="auth_required",
                     reauth_notified_at=now, updated_at=now))
            notify_now = claimed.rowcount == 1
            if not notify_now:
                await db.execute(update(GoogleAccount).where(
                    GoogleAccount.account_id == account_id,
                ).values(state="reauth_required", message=message, last_error_code="auth_required", updated_at=now))
            await db.commit()
        metrics.incr("reauth_required")
        metrics.event("reauth_required", feature=feature, first=notify_now)
        await registry.fire("on_reauth", account_id)  # 다른 기능도 만료된 세션을 쥔 클라이언트를 닫는다
        if notify_now:
            with contextlib.suppress(Exception):
                from ttakkari.push.service import PushKind, notify

                who = registry.get(feature).label if feature else "Google 계정 연동"
                await notify(PushKind.google_account, "Google 계정 다시 로그인해 주세요",
                             f"{who}의 로그인 세션이 만료됐습니다. 설정의 Google 계정에서 로그인 창을 열어 주세요.",
                             url=ACCOUNT_PAGE, dedupe_key=f"google_account:reauth:{account_id}:{int(time.time() // 60)}")

    # ------------------------------------------------------------------ 기능 동작 위임

    async def verify(self, account_id: str, feature: str) -> dict[str, Any]:
        c = registry.get(feature)
        if c.verify is None:
            raise errors.Unsupported(f"{c.label} 은(는) 접속 확인을 지원하지 않습니다.")
        return await c.verify(account_id)

    async def resume(self, account_id: str, feature: str) -> dict[str, Any]:
        c = registry.get(feature)
        if c.resume is None:
            raise errors.Unsupported(f"{c.label} 은(는) 재개할 멈춤 상태가 없습니다.")
        return await c.resume(account_id)

    async def shutdown(self) -> None:
        await login.manager.shutdown()


service = GoogleAccountService()
