"""NotebookLMService: REST·MCP 가 공유하는 단일 진입점(ADR 0045). 상류 타입을 모르고 NotebookLMBackend 만 안다.

정책은 여기서만 정한다.
- 계정당 동시 실행 수 제한(기본 2). 대기하다 시간이 지나면 Busy.
- 읽기만 지수 백오프로 재시도(최대 3회, 대기 상한). 쓰기·질의는 한 번만 보내고 결과가 불명이면 outcome_unknown 으로 알린다.
- 429 는 Retry-After(없으면 기본값)만큼 쿨다운으로 기록하고 그동안 Google 에 요청하지 않는다. 우회·재시도 없음.
- 로그인 만료는 '재인증 필요'로 바꾸고 알림을 한 번 보낸다. 보호 신호는 blocked 로 멈추고 사람이 resume 할 때까지 요청하지 않는다.
- 소스 추가는 중복 원장에 먼저 예약하고, 장시간 처리 대기는 DB job 으로 돌린다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import NotebookLMAccount, NotebookLMJob, NotebookLMSource
from ttakkari.notebooklm import dedupe, errors, login, metrics, redact, session
from ttakkari.notebooklm.export import build_ledger_export
from ttakkari.notebooklm.ports import (
    NotebookDetail,
    NotebookLMBackend,
    NotebookSummary,
    QueryResult,
    SourceInfo,
)

log = logging.getLogger(__name__)

DEFAULT_ACCOUNT = session.DEFAULT_ACCOUNT
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "unknown"})
HEARTBEAT_SECONDS = 5.0
MAX_TITLE = 200
MAX_TEXT_CHARS = 500_000
MAX_QUESTION_CHARS = 8000
JOB_QUEUE_WAIT_SECONDS = 600

BackendFactory = Callable[[str], NotebookLMBackend]


def _now() -> datetime:
    return datetime.now(UTC)


def default_backend_factory(account_id: str) -> NotebookLMBackend:
    from ttakkari.notebooklm.adapters.notebooklm_py import NotebookLMPyBackend

    s = get_settings()
    return NotebookLMPyBackend(account_id, timeout=float(s.notebooklm_request_timeout_seconds),
                               chat_timeout=float(s.notebooklm_query_timeout_seconds))


def backoff_delay(attempt: int, base: float, cap: float, *, jitter: bool = True) -> float:
    """읽기 재시도 대기. 지수 증가에 상한을 두고 0~25% 지터를 더한다."""
    delay = min(cap, base * (2 ** attempt))
    return delay + (random.uniform(0, delay * 0.25) if jitter else 0.0)


def job_out(row: NotebookLMJob) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "account_id": row.account_id,
        "kind": row.kind,
        "notebook_id": row.notebook_id,
        "status": row.status,
        "params": row.params,
        "result": row.result,
        "error_code": row.error_code,
        "error": row.error,
        "cancel_requested": row.cancel_requested,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


class NotebookLMService:
    def __init__(self, backend_factory: BackendFactory | None = None) -> None:
        self._factory: BackendFactory = backend_factory or default_backend_factory
        self._backends: dict[str, NotebookLMBackend] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}
        self._last_verify: dict[str, float] = {}

    # ------------------------------------------------------------------ 구성

    def set_backend_factory(self, factory: BackendFactory | None) -> None:
        """백엔드 구현 교체 지점. 테스트는 가짜를, 나중의 다른 구현은 같은 Protocol 로 끼운다."""
        self._factory = factory or default_backend_factory
        self._backends.clear()

    def _backend(self, account_id: str) -> NotebookLMBackend:
        b = self._backends.get(account_id)
        if b is None:
            b = self._backends[account_id] = self._factory(account_id)
        return b

    async def invalidate(self, account_id: str) -> None:
        """세션이 바뀌었으니 열려 있는 클라이언트를 닫는다."""
        b = self._backends.pop(account_id, None)
        if b is not None:
            with contextlib.suppress(Exception):
                await b.aclose()

    def _sem(self, account_id: str) -> asyncio.Semaphore:
        sem = self._sems.get(account_id)
        if sem is None:
            sem = self._sems[account_id] = asyncio.Semaphore(get_settings().notebooklm_concurrency)
        return sem

    async def shutdown(self) -> None:
        for t in list(self._tasks.values()):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        for account_id in list(self._backends):
            await self.invalidate(account_id)
        await login.manager.shutdown()

    # ------------------------------------------------------------------ 계정 상태

    async def _row(self, db, account_id: str) -> NotebookLMAccount:
        session.check_account(account_id)
        row = await db.get(NotebookLMAccount, account_id)
        if row is None:
            await db.execute(insert(NotebookLMAccount).values(
                account_id=account_id, state="login_required", created_at=_now(), updated_at=_now()
            ).on_conflict_do_nothing())
            await db.commit()
            row = await db.get(NotebookLMAccount, account_id)
        assert row is not None
        return row

    async def _set_state(self, account_id: str, state: str, message: str | None = None, **fields: Any) -> None:
        async with sessionmaker()() as db:
            row = await self._row(db, account_id)
            row.state, row.message = state, message
            for k, v in fields.items():
                setattr(row, k, v)
            await db.commit()

    def _effective(self, row: NotebookLMAccount) -> tuple[str, str | None]:
        """저장된 상태 + 세션 파일 + 쿨다운 만료를 합친 현재 상태."""
        if not get_settings().notebooklm_enabled:
            return "disabled", "NotebookLM 연결이 꺼져 있습니다."
        if not session.exists(row.account_id):
            return "login_required", "로그인한 적이 없습니다. 로그인 창을 열어 한 번 로그인해 주세요."
        if row.state == "blocked":
            return "blocked", row.message
        if row.state == "reauth_required":
            return "reauth_required", row.message
        if row.cooldown_until and row.cooldown_until > _now():
            return "cooldown", row.message
        if row.state == "cooldown":  # 쿨다운이 끝났다. 이전에 성공한 적이 있으면 바로 쓸 수 있다.
            return ("ready", None) if row.last_ok_at else ("session_saved", "쿨다운이 끝났습니다.")
        if row.state == "login_required":
            return "session_saved", "세션은 저장됐지만 아직 NotebookLM 접속을 확인하지 않았습니다."
        return row.state, row.message

    async def status(self, account_id: str = DEFAULT_ACCOUNT) -> dict[str, Any]:
        """브라우저·Google 요청 없이 마지막으로 알려진 상태만 본다."""
        s = get_settings()
        async with sessionmaker()() as db:
            row = await self._row(db, account_id)
            state, message = self._effective(row)
            cooldown_left = None
            if state == "cooldown" and row.cooldown_until:
                cooldown_left = max(1, int((row.cooldown_until - _now()).total_seconds()))
            backend = self._backend(account_id)
            return {
                "account_id": account_id,
                "state": state,
                "message": message,
                "logged_in": None if state in {"session_saved", "disabled"} else state in {"ready", "cooldown"},
                "reauth_required": state == "reauth_required",
                "cooldown_until": row.cooldown_until.isoformat() if state == "cooldown" and row.cooldown_until else None,
                "retry_after": cooldown_left,
                "last_ok_at": row.last_ok_at.isoformat() if row.last_ok_at else None,
                "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
                "last_error_code": row.last_error_code,
                "session": {
                    "stored": session.exists(account_id),
                    "cookie_names": session.cookie_names(account_id),  # 이름만. 값은 어디에도 없다.
                    "permissions_ok": session.mode_ok(account_id) if session.exists(account_id) else None,
                    "encrypted": False,
                    "protection": "소유자 전용 파일 권한(0600/0700)만 사용합니다. 암호화하지 않습니다.",
                },
                "login": login.manager.state(account_id).to_dict(),
                "browser_available": await asyncio.to_thread(login.find_chrome) is not None,
                "backend": backend.name,
                "capabilities": [c.model_dump() for c in backend.capabilities()],
                "limits": {
                    "concurrency": s.notebooklm_concurrency,
                    "read_retry_max": s.notebooklm_read_retry_max,
                    "request_timeout_seconds": s.notebooklm_request_timeout_seconds,
                    "query_timeout_seconds": s.notebooklm_query_timeout_seconds,
                },
                "metrics": metrics.snapshot(),
            }

    async def start_login(self, account_id: str = DEFAULT_ACCOUNT) -> dict[str, Any]:
        st = await login.manager.start(account_id, on_done=self._on_login_done)
        return st.to_dict()

    async def cancel_login(self, account_id: str = DEFAULT_ACCOUNT) -> dict[str, Any]:
        return (await login.manager.cancel(account_id)).to_dict()

    async def _on_login_done(self, st: login.LoginState) -> None:
        if st.state != "done":
            return
        await self.invalidate(st.account_id)
        await self._set_state(st.account_id, "session_saved", "로그인 세션을 저장했습니다. 접속을 확인하는 중입니다.",
                              reauth_notified_at=None, cooldown_until=None, last_error_code=None)
        self._last_verify.pop(st.account_id, None)
        try:
            await self.verify(st.account_id)
        except errors.NotebookLMError as e:
            log.info("notebooklm post-login verify: %s", e.code)

    async def delete_session(self, account_id: str = DEFAULT_ACCOUNT) -> dict[str, Any]:
        """저장된 세션 파일을 지우고 로그인 필요 상태로 돌린다(Google 쪽 로그아웃은 아님)."""
        await self.invalidate(account_id)
        session.delete(account_id)
        await self._set_state(account_id, "login_required", "저장된 세션을 삭제했습니다.",
                              reauth_notified_at=None, cooldown_until=None)
        return await self.status(account_id)

    async def resume(self, account_id: str = DEFAULT_ACCOUNT) -> dict[str, Any]:
        """blocked(보호 신호)를 사람이 확인한 뒤 풀어 준다. Google 쪽 문제를 해결하는 것이 아니라 자동 요청 재개 표시다."""
        async with sessionmaker()() as db:
            row = await self._row(db, account_id)
            if row.state != "blocked":
                raise errors.Busy("멈춰 있는 상태가 아닙니다.")
        await self._set_state(account_id, "session_saved" if session.exists(account_id) else "login_required",
                              "사람이 확인한 뒤 재개했습니다.", last_error_code=None)
        return await self.status(account_id)

    async def verify(self, account_id: str = DEFAULT_ACCOUNT) -> dict[str, Any]:
        """저장된 세션으로 NotebookLM 에 접속해 본다(Google 요청 1회). 짧은 간격으로 반복하지 못한다."""
        s = get_settings()
        last = self._last_verify.get(account_id)
        if last is not None and time.monotonic() - last < s.notebooklm_verify_interval_seconds:
            raise errors.RateLimited(
                f"확인은 {s.notebooklm_verify_interval_seconds}초에 한 번만 할 수 있습니다.",
                retry_after=int(s.notebooklm_verify_interval_seconds - (time.monotonic() - last)) + 1)
        self._last_verify[account_id] = time.monotonic()
        await self._call(account_id, "verify", lambda b: b.verify_session())
        async with sessionmaker()() as db:
            row = await self._row(db, account_id)
            row.last_checked_at = _now()
            await db.commit()
        return await self.status(account_id)

    async def _admit(self, account_id: str) -> None:
        """Google 에 나가기 전 검사. 막힌 상태면 요청을 보내지 않고 이유를 알린다."""
        if not get_settings().notebooklm_enabled:
            raise errors.Disabled("NotebookLM 연결이 꺼져 있습니다.")
        async with sessionmaker()() as db:
            row = await self._row(db, account_id)
            state, message = self._effective(row)
            cooldown_until = row.cooldown_until
        if state == "login_required":
            raise errors.AuthRequired(message or "로그인이 필요합니다.")
        if state == "reauth_required":
            raise errors.AuthRequired(message or "로그인이 만료됐습니다. 다시 로그인해 주세요.")
        if state == "blocked":
            raise errors.ProtectionSignal(message or "보호 신호로 멈춰 있습니다. 직접 확인한 뒤 재개해 주세요.")
        if state == "cooldown" and cooldown_until:
            wait = max(1, int((cooldown_until - _now()).total_seconds()))
            metrics.incr("cooldown_rejected")
            raise errors.Cooldown(f"NotebookLM 사용 제한으로 쉬는 중입니다. {wait}초 뒤에 다시 시도해 주세요.",
                                  retry_after=wait)

    async def _on_error(self, account_id: str, e: errors.NotebookLMError) -> None:
        if isinstance(e, errors.AuthRequired):
            await self.invalidate(account_id)
            await self._mark_reauth(account_id, e.message)
        elif isinstance(e, errors.Cooldown):
            return
        elif isinstance(e, errors.RateLimited):
            wait = e.retry_after or get_settings().notebooklm_default_cooldown_seconds
            metrics.incr("rate_limited")
            metrics.event("cooldown_start", seconds=wait)
            await self._set_state(account_id, "cooldown", f"NotebookLM 사용 제한으로 {wait}초 쉽니다.",
                                  cooldown_until=_now() + timedelta(seconds=wait), last_error_code=e.code)
            e.retry_after = wait
        elif isinstance(e, errors.ProtectionSignal):
            metrics.incr("protection_signals")
            await self._set_state(account_id, "blocked", e.message, last_error_code=e.code)
        else:
            async with sessionmaker()() as db:
                row = await self._row(db, account_id)
                row.last_error_code = e.code
                await db.commit()

    async def _mark_reauth(self, account_id: str, message: str) -> None:
        async with sessionmaker()() as db:
            await self._row(db, account_id)
            now = _now()
            # 여러 요청이 동시에 만료를 감지해도 DB에서 한 요청만 알림 발송권을 얻는다.
            claimed = await db.execute(update(NotebookLMAccount).where(
                NotebookLMAccount.account_id == account_id,
                NotebookLMAccount.reauth_notified_at.is_(None),
            ).values(state="reauth_required", message=message, last_error_code="auth_required",
                     reauth_notified_at=now, updated_at=now))
            notify_now = claimed.rowcount == 1
            if not notify_now:
                await db.execute(update(NotebookLMAccount).where(
                    NotebookLMAccount.account_id == account_id,
                ).values(state="reauth_required", message=message, last_error_code="auth_required", updated_at=now))
            await db.commit()
        metrics.incr("reauth_required")
        if notify_now:
            with contextlib.suppress(Exception):
                from ttakkari.push.service import PushKind, notify

                await notify(PushKind.notebooklm, "NotebookLM 다시 로그인해 주세요",
                             "로그인 세션이 만료됐습니다. NotebookLM 화면에서 로그인 창을 열어 주세요.",
                             url="/notebooklm", dedupe_key=f"notebooklm:reauth:{account_id}:{int(time.time() // 60)}")

    async def _on_success(self, account_id: str) -> None:
        async with sessionmaker()() as db:
            await self._row(db, account_id)
            now = _now()
            # A slower concurrent success must not overwrite a 429/protection/auth
            # failure committed by another request while this call was in flight.
            await db.execute(update(NotebookLMAccount).where(
                NotebookLMAccount.account_id == account_id,
                NotebookLMAccount.state.notin_(("blocked", "reauth_required")),
                or_(NotebookLMAccount.state != "cooldown", NotebookLMAccount.cooldown_until.is_(None),
                    NotebookLMAccount.cooldown_until <= now),
            ).values(state="ready", message=None, last_error_code=None, cooldown_until=None,
                     reauth_notified_at=None, last_ok_at=now, updated_at=now))
            await db.commit()

    # ------------------------------------------------------------------ 호출 정책

    async def _call(
        self, account_id: str, op: str, fn: Callable[[NotebookLMBackend], Awaitable[Any]], *,
        write: bool = False, timeout: float | None = None, queue_wait: float | None = None, retry: bool = True,
    ) -> Any:
        s = get_settings()
        await self._admit(account_id)
        sem = self._sem(account_id)
        try:
            await asyncio.wait_for(sem.acquire(), queue_wait if queue_wait is not None else s.notebooklm_request_timeout_seconds)
        except TimeoutError:
            metrics.incr("busy_rejected")
            raise errors.Busy("NotebookLM 요청이 몰려 있습니다. 잠시 뒤 다시 시도해 주세요.", retry_after=5) from None
        try:
            # Semaphore wait 동안 다른 요청이 429/보호 신호를 기록했을 수 있다.
            # 계정 상태를 전송 직전에 다시 읽어 쿨다운·blocked 진입 후의 대기 요청을 막는다.
            await self._admit(account_id)
            attempt = 0
            max_retry = 0 if (write or not retry) else s.notebooklm_read_retry_max
            while True:
                if attempt:  # 대기하는 사이 쿨다운·재인증 상태가 바뀌었을 수 있다
                    await self._admit(account_id)
                started = time.monotonic()
                metrics.incr(f"requests.{op}")
                try:
                    async with asyncio.timeout(timeout or s.notebooklm_request_timeout_seconds):
                        result = await fn(self._backend(account_id))
                except TimeoutError:
                    err: errors.NotebookLMError = errors.Timeout("NotebookLM 응답을 기다리다 시간이 지났습니다.",
                                                                outcome_unknown=write)
                except errors.NotebookLMError as e:
                    err = e
                else:
                    elapsed = (time.monotonic() - started) * 1000
                    metrics.observe(op, elapsed)
                    metrics.event("request", op=op, ok=True, ms=int(elapsed), attempt=attempt + 1, write=write)
                    await self._on_success(account_id)
                    return result
                metrics.observe(op, (time.monotonic() - started) * 1000)
                metrics.incr(f"errors.{err.code}")
                metrics.event("request", op=op, ok=False, code=err.code, attempt=attempt + 1, write=write)
                if err.retryable and not write and attempt < max_retry:
                    delay = backoff_delay(attempt, s.notebooklm_read_retry_base_seconds, s.notebooklm_read_retry_cap_seconds)
                    attempt += 1
                    metrics.incr("retries")
                    metrics.event("retry", op=op, attempt=attempt, delay_ms=int(delay * 1000))
                    await asyncio.sleep(delay)
                    continue
                await self._on_error(account_id, err)
                raise err
        finally:
            sem.release()

    # ------------------------------------------------------------------ 노트북

    async def list_notebooks(self, account_id: str = DEFAULT_ACCOUNT) -> list[NotebookSummary]:
        return await self._call(account_id, "notebooks.list", lambda b: b.list_notebooks())

    async def get_notebook(self, notebook_id: str, account_id: str = DEFAULT_ACCOUNT) -> NotebookDetail:
        return await self._call(account_id, "notebooks.get", lambda b: b.get_notebook(notebook_id))

    @staticmethod
    def _title(title: str) -> str:
        t = (title or "").strip()
        if not t or len(t) > MAX_TITLE:
            raise errors.Invalid(f"제목은 1~{MAX_TITLE}자여야 합니다.")
        return t

    async def create_notebook(self, title: str, account_id: str = DEFAULT_ACCOUNT) -> NotebookSummary:
        t = self._title(title)
        return await self._call(account_id, "notebooks.create", lambda b: b.create_notebook(t), write=True)

    async def rename_notebook(self, notebook_id: str, title: str, account_id: str = DEFAULT_ACCOUNT) -> NotebookSummary:
        t = self._title(title)
        return await self._call(account_id, "notebooks.rename", lambda b: b.rename_notebook(notebook_id, t), write=True)

    async def delete_notebook(self, notebook_id: str, account_id: str = DEFAULT_ACCOUNT) -> None:
        await self._call(account_id, "notebooks.delete", lambda b: b.delete_notebook(notebook_id), write=True)
        async with sessionmaker()() as db:
            await db.execute(delete(NotebookLMSource).where(
                NotebookLMSource.account_id == account_id, NotebookLMSource.notebook_id == notebook_id))
            await db.commit()

    # ------------------------------------------------------------------ 소스

    async def list_sources(self, notebook_id: str, account_id: str = DEFAULT_ACCOUNT) -> list[SourceInfo]:
        return await self._call(account_id, "sources.list", lambda b: b.list_sources(notebook_id))

    async def delete_source(self, notebook_id: str, source_id: str, account_id: str = DEFAULT_ACCOUNT) -> None:
        await self._call(account_id, "sources.delete", lambda b: b.delete_source(notebook_id, source_id), write=True)
        async with sessionmaker()() as db:
            await db.execute(delete(NotebookLMSource).where(
                NotebookLMSource.account_id == account_id, NotebookLMSource.notebook_id == notebook_id,
                NotebookLMSource.remote_source_id == source_id))
            await db.commit()

    async def add_source(self, notebook_id: str, kind: str, *, file_path: Path | None = None, **kw: Any) -> dict[str, Any]:
        """소스 등록을 job 으로 시작한다. 이미 등록된 소스면 job 없이 {"duplicate": true, "source": ...} 를 돌려준다."""
        started = False
        try:
            out = await self._add_source(notebook_id, kind, file_path=file_path, **kw)
            started = out.get("job") is not None
            return out
        finally:
            if file_path is not None and not started:  # job 이 임시 업로드 폴더를 맡지 못했으면 여기서 지운다
                shutil.rmtree(file_path.parent, ignore_errors=True)

    async def _add_source(
        self, notebook_id: str, kind: str, *, account_id: str = DEFAULT_ACCOUNT, url: str | None = None,
        title: str | None = None, text: str | None = None, file_path: Path | None = None,
        file_sha256: str | None = None, force: bool = False,
    ) -> dict[str, Any]:
        session.check_account(account_id)
        if not get_settings().notebooklm_enabled:
            raise errors.Disabled("NotebookLM 연결이 꺼져 있습니다.")
        if kind == "url":
            if not url:
                raise errors.Invalid("url 이 필요합니다.")
            norm, real_kind = dedupe.normalize_url(url)
            key, job_kind, params = dedupe.url_key(norm), "source_add_url", {"url": norm, "kind": real_kind}
        elif kind == "text":
            body = (text or "").strip()
            if not body or len(body) > MAX_TEXT_CHARS:
                raise errors.Invalid(f"텍스트는 1~{MAX_TEXT_CHARS}자여야 합니다.")
            if not (title or "").strip():
                raise errors.Invalid("텍스트 소스에는 제목이 필요합니다.")
            key, real_kind, job_kind = dedupe.text_key(body), "text", "source_add_text"
            params = {"title": title.strip()[:MAX_TITLE], "chars": len(body)}
        elif kind == "file":
            if file_path is None or not file_sha256:
                raise errors.Invalid("파일이 필요합니다.")
            key, real_kind, job_kind = dedupe.file_key(file_sha256), "file", "source_add_file"
            params = {"file_name": file_path.name[:120], "sha256": file_sha256}
        else:
            raise errors.Invalid("kind 는 url, text, file 중 하나여야 합니다.")
        await self._admit(account_id)  # 막힌 상태면 예약도 만들지 않는다

        job_id = uuid.uuid4()
        existing = await self._reserve(account_id, notebook_id, key, real_kind, params.get("title") or params.get("url"),
                                       job_id, force=force)
        if existing is not None:
            return existing
        async with sessionmaker()() as db:
            db.add(NotebookLMJob(id=job_id, account_id=account_id, kind=job_kind, notebook_id=notebook_id,
                                 status="queued", params=params, heartbeat_at=_now()))
            await db.commit()
        spec = {"kind": kind, "url": params.get("url"), "title": (title or "").strip() or None, "text": text,
                "file_path": file_path, "key": key}
        task = asyncio.create_task(self._run_source_job(job_id, account_id, notebook_id, spec))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._tasks.pop(jid, None))
        metrics.incr("jobs.started")
        return {"duplicate": False, "job": await self.get_job(job_id), "source": None}

    async def _reserve(self, account_id: str, notebook_id: str, key: str, kind: str, title: str | None,
                       job_id: uuid.UUID, *, force: bool) -> dict[str, Any] | None:
        """예약 행을 만든다. 이미 있으면 상태별로 중복 응답 또는 오류. 새로 만들었으면 None."""
        for _ in range(2):
            async with sessionmaker()() as db:
                row = NotebookLMSource(account_id=account_id, notebook_id=notebook_id, dedupe_key=key, kind=kind,
                                       title=(title or "")[:300] or None, status="reserved", job_id=job_id)
                db.add(row)
                try:
                    await db.commit()
                    return None
                except IntegrityError:
                    await db.rollback()
                cur = await db.scalar(select(NotebookLMSource).where(
                    NotebookLMSource.account_id == account_id, NotebookLMSource.notebook_id == notebook_id,
                    NotebookLMSource.dedupe_key == key))
                if cur is None:
                    continue
                if cur.status == "added":
                    metrics.incr("duplicates_blocked")
                    return {"duplicate": True, "job": None,
                            "source": {"id": cur.remote_source_id, "title": cur.title, "kind": cur.kind}}
                if cur.status == "reserved":
                    metrics.incr("duplicates_blocked")
                    raise errors.Duplicate("같은 소스를 등록하는 작업이 진행 중입니다.")
                if cur.status == "unknown" and not force:
                    found = await self._reconcile(account_id, notebook_id, cur)
                    if found is not None:
                        return found
                    raise errors.Duplicate(
                        "이전 등록 요청의 결과를 알 수 없습니다. 노트북의 소스 목록을 확인하고, 없을 때만 force=true 로 다시 등록하세요.")
                await db.delete(cur)  # failed 이거나 force 로 풀어 준 unknown
                await db.commit()
        raise errors.Duplicate("소스 예약에 실패했습니다. 잠시 뒤 다시 시도해 주세요.")

    async def _reconcile(self, account_id: str, notebook_id: str, row: NotebookLMSource) -> dict[str, Any] | None:
        """결과 불명(unknown) URL 소스를 원격 목록과 대조한다. 같은 정규화 URL 이 있으면 등록된 것으로 확정한다."""
        if row.kind not in {"url", "youtube"}:
            return None
        sources = await self.list_sources(notebook_id, account_id)
        for src in sources:
            if not src.url:
                continue
            with contextlib.suppress(errors.Invalid):
                norm, _ = dedupe.normalize_url(src.url)
                if dedupe.url_key(norm) == row.dedupe_key:
                    async with sessionmaker()() as db:
                        cur = await db.get(NotebookLMSource, row.id)
                        if cur is not None:
                            cur.status, cur.remote_source_id, cur.title = "added", src.id, src.title or cur.title
                            await db.commit()
                    return {"duplicate": True, "job": None, "source": src.model_dump()}
        return None

    async def _release(self, account_id: str, notebook_id: str, key: str) -> None:
        async with sessionmaker()() as db:
            await db.execute(delete(NotebookLMSource).where(
                NotebookLMSource.account_id == account_id, NotebookLMSource.notebook_id == notebook_id,
                NotebookLMSource.dedupe_key == key, NotebookLMSource.status == "reserved"))
            await db.commit()

    async def _ledger(self, account_id: str, notebook_id: str, key: str, status: str, **fields: Any) -> None:
        async with sessionmaker()() as db:
            await db.execute(update(NotebookLMSource).where(
                NotebookLMSource.account_id == account_id, NotebookLMSource.notebook_id == notebook_id,
                NotebookLMSource.dedupe_key == key).values(status=status, updated_at=_now(), **fields))
            await db.commit()

    # ------------------------------------------------------------------ job

    async def _job_update(self, job_id: uuid.UUID, **fields: Any) -> None:
        async with sessionmaker()() as db:
            await db.execute(update(NotebookLMJob).where(NotebookLMJob.id == job_id).values(updated_at=_now(), **fields))
            await db.commit()

    async def _finish(self, job_id: uuid.UUID, status: str, *, result: dict | None = None, error: str | None = None,
                      code: str | None = None) -> None:
        await self._job_update(job_id, status=status, result=result, finished_at=_now(),
                               error=redact.mask(error)[:500] if error else None, error_code=code)
        metrics.incr(f"jobs.{status}")
        metrics.event("job_finished", job=str(job_id), status=status, code=code)

    async def _heartbeat(self, job_id: uuid.UUID, main: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                async with sessionmaker()() as db:
                    await db.execute(update(NotebookLMJob).where(NotebookLMJob.id == job_id).values(heartbeat_at=_now()))
                    cancel = await db.scalar(select(NotebookLMJob.cancel_requested).where(NotebookLMJob.id == job_id))
                    await db.commit()
                if cancel:
                    main.cancel()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- 하트비트 실패가 작업을 죽이지 않게
                log.warning("notebooklm heartbeat failed")

    async def _run_source_job(self, job_id: uuid.UUID, account_id: str, notebook_id: str, spec: dict[str, Any]) -> None:
        s = get_settings()
        key = spec["key"]
        phase = "queued"  # queued: 아직 Google 에 아무것도 보내지 않음 → write: 등록 요청 전송 중 → wait: 등록됨, 처리 대기
        added: SourceInfo | None = None
        hb = asyncio.create_task(self._heartbeat(job_id, asyncio.current_task()))  # type: ignore[arg-type]
        try:
            kind = spec["kind"]

            async def add(b: NotebookLMBackend) -> SourceInfo:
                nonlocal phase
                phase = "write"  # 세마포어·상태 검사를 지나 실제로 전송을 시작하는 순간
                if kind == "url":
                    return await b.add_url(notebook_id, spec["url"], title=spec["title"])
                if kind == "text":
                    return await b.add_text(notebook_id, spec["title"], spec["text"])
                return await b.add_file(notebook_id, spec["file_path"], title=spec["title"])

            await self._job_update(job_id, status="running", started_at=_now(), heartbeat_at=_now())
            try:
                added = await self._call(account_id, f"source_add.{kind}", add, write=True,
                                         timeout=float(s.notebooklm_request_timeout_seconds) * 4,
                                         queue_wait=JOB_QUEUE_WAIT_SECONDS)
            except errors.NotebookLMError as e:
                if e.outcome_unknown:
                    await self._ledger(account_id, notebook_id, key, "unknown")
                    await self._finish(job_id, "unknown", error=e.message, code=e.code,
                                       result={"outcome_unknown": True})
                else:
                    await self._release(account_id, notebook_id, key)
                    await self._finish(job_id, "failed", error=e.message, code=e.code)
                return
            await self._ledger(account_id, notebook_id, key, "added", remote_source_id=added.id,
                               title=(added.title or "")[:300] or None)
            phase = "wait"
            try:
                ready = await self._call(
                    account_id, "source_wait", lambda b: b.wait_source_ready(
                        notebook_id, added.id, timeout=float(s.notebooklm_source_wait_seconds)),
                    timeout=float(s.notebooklm_source_wait_seconds) + 15, queue_wait=JOB_QUEUE_WAIT_SECONDS, retry=False)
                await self._finish(job_id, "succeeded", result={"source": ready.model_dump(), "ready": True})
            except errors.Timeout:
                await self._finish(job_id, "succeeded", result={
                    "source": added.model_dump(), "ready": False,
                    "note": "등록은 됐으나 처리 대기 시간이 지났습니다. 소스 목록에서 상태를 확인하세요."})
            except errors.NotebookLMError as e:
                if isinstance(e, errors.UpstreamRejected):
                    await self._finish(job_id, "failed", error=e.message, code="source_error",
                                       result={"source": added.model_dump(), "ready": False})
                else:
                    await self._finish(job_id, "succeeded", result={
                        "source": added.model_dump(), "ready": False,
                        "note": f"등록은 됐으나 처리 상태를 확인하지 못했습니다({e.code})."})
        except asyncio.CancelledError:
            await asyncio.shield(self._on_job_cancelled(job_id, account_id, notebook_id, key, phase, added))
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("notebooklm job crashed: %s", redact.safe_error(e))
            with contextlib.suppress(Exception):
                if phase == "write":
                    await self._ledger(account_id, notebook_id, key, "unknown")
                await self._finish(job_id, "unknown" if phase != "queued" else "failed",
                                   error="작업 중 예상하지 못한 오류가 났습니다.", code="internal")
        finally:
            hb.cancel()
            await asyncio.gather(hb, return_exceptions=True)
            fp = spec.get("file_path")
            if fp is not None:
                shutil.rmtree(Path(fp).parent, ignore_errors=True)  # 업로드 임시 폴더(job 단위)

    async def _on_job_cancelled(self, job_id: uuid.UUID, account_id: str, notebook_id: str, key: str, phase: str,
                                added: SourceInfo | None) -> None:
        with contextlib.suppress(Exception):
            async with sessionmaker()() as db:
                requested = bool(await db.scalar(select(NotebookLMJob.cancel_requested).where(NotebookLMJob.id == job_id)))
            if phase == "write":
                await self._ledger(account_id, notebook_id, key, "unknown")
            elif phase == "queued":
                await self._release(account_id, notebook_id, key)
            result = {"outcome_unknown": True} if phase == "write" else (
                {"source": added.model_dump(), "ready": False} if added else None)
            if requested:
                await self._finish(job_id, "cancelled", result=result, error="사용자가 취소했습니다.", code="cancelled")
            elif phase == "queued":
                await self._finish(job_id, "failed", error="시작 전에 서버가 종료되어 실행되지 않았습니다.", code="shutdown")
            else:
                await self._finish(job_id, "unknown", result=result, error="서버 종료 등으로 중단되어 결과를 알 수 없습니다.",
                                   code="interrupted")

    async def get_job(self, job_id: uuid.UUID | str, account_id: str | None = None) -> dict[str, Any]:
        try:
            jid = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
        except ValueError:
            raise errors.NotFound("작업을 찾지 못했습니다.") from None
        async with sessionmaker()() as db:
            row = await db.get(NotebookLMJob, jid)
            if row is None or (account_id and row.account_id != account_id):
                raise errors.NotFound("작업을 찾지 못했습니다.")
            return job_out(row)

    async def list_jobs(self, account_id: str = DEFAULT_ACCOUNT, limit: int = 20) -> list[dict[str, Any]]:
        async with sessionmaker()() as db:
            rows = await db.scalars(select(NotebookLMJob).where(NotebookLMJob.account_id == account_id)
                                    .order_by(NotebookLMJob.created_at.desc()).limit(max(1, min(limit, 100))))
            return [job_out(r) for r in rows]

    async def cancel_job(self, job_id: uuid.UUID | str, account_id: str = DEFAULT_ACCOUNT) -> dict[str, Any]:
        cur = await self.get_job(job_id, account_id)
        if cur["status"] in TERMINAL:
            raise errors.Busy("이미 끝난 작업입니다.")
        jid = uuid.UUID(cur["id"])
        await self._job_update(jid, cancel_requested=True)
        task = self._tasks.get(jid)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), 10)
        return await self.get_job(jid, account_id)

    async def recover_jobs(self, *, stale_seconds: int | None = None) -> int:
        """멈춘 job 정리. running 은 unknown(결과 불명), queued 는 failed(시작 전). 소스 원장도 같이 맞춘다.

        stale_seconds=None 이면 상태만 보고 모두 정리한다(단일 프로세스 기동 직후). 숫자면 하트비트가 그보다 오래된 것만 정리한다
        (무중단 배포에서 이전 세대가 아직 돌리는 job 을 건드리지 않는다)."""
        cutoff = None if stale_seconds is None else _now() - timedelta(seconds=stale_seconds)
        changed = 0
        async with sessionmaker()() as db:
            q = select(NotebookLMJob).where(NotebookLMJob.status.in_(("queued", "running")))
            if cutoff is not None:
                q = q.where(NotebookLMJob.heartbeat_at < cutoff)
            rows = list(await db.scalars(q))
            for row in rows:
                if row.id in self._tasks:
                    continue
                was_running = row.status == "running"
                row.status = "unknown" if was_running else "failed"
                row.error_code = "interrupted" if was_running else "shutdown"
                row.error = ("서버 재시작 등으로 중단되어 결과를 알 수 없습니다. 소스 목록을 확인하세요." if was_running
                             else "시작 전에 중단되어 실행되지 않았습니다. 다시 요청하세요.")
                row.finished_at = _now()
                src = await db.scalar(select(NotebookLMSource).where(NotebookLMSource.job_id == row.id))
                if src is not None and src.status == "reserved":
                    if was_running:
                        src.status = "unknown"
                    else:
                        await db.delete(src)
                changed += 1
            await db.commit()
        # 오래된 업로드 임시 폴더
        up = session.uploads_dir()
        if up.exists():
            for d in up.iterdir():
                with contextlib.suppress(OSError):
                    if d.is_dir() and time.time() - d.stat().st_mtime > 24 * 3600:
                        shutil.rmtree(d, ignore_errors=True)
        if changed:
            metrics.incr("jobs.recovered", changed)
        return changed

    # ------------------------------------------------------------------ 질의

    async def query(
        self, notebook_id: str, question: str, *, account_id: str = DEFAULT_ACCOUNT,
        conversation_id: str | None = None, source_ids: list[str] | None = None,
    ) -> QueryResult:
        q = (question or "").strip()
        if not q or len(q) > MAX_QUESTION_CHARS:
            raise errors.Invalid(f"질문은 1~{MAX_QUESTION_CHARS}자여야 합니다.")
        s = get_settings()
        result: QueryResult = await self._call(
            account_id, "query", lambda b: b.ask(notebook_id, q, conversation_id=conversation_id, source_ids=source_ids),
            write=True, timeout=float(s.notebooklm_query_timeout_seconds))
        # 인용 출처의 제목·URL 을 채운다. 실패해도 질의 결과는 돌려준다(읽기 1회, 재시도 포함 정책 동일).
        if result.sources:
            with contextlib.suppress(errors.NotebookLMError):
                known = {x.id: x for x in await self.list_sources(notebook_id, account_id)}
                for ref in result.sources:
                    src = known.get(ref.id)
                    if src is not None:
                        ref.title, ref.url, ref.kind = src.title, src.url, src.kind
        return result

    async def query_with_export(self, notebook_id: str, question: str, **kw: Any) -> tuple[QueryResult, dict[str, Any]]:
        result = await self.query(notebook_id, question, **kw)
        return result, build_ledger_export(result, account_id=kw.get("account_id", DEFAULT_ACCOUNT))


service = NotebookLMService()
