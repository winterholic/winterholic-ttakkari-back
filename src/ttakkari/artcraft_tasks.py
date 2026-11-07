"""ArtCraft 작업(ADR 0039): 사용자의 창작 지시를 기존 세션·Run·Artifact 위에서 돌린다.

새 실행기는 없다. 작업 = ``sessions`` 행 하나(``kind=chat``, 활성 개인 계정에서 선택한 엔진) + ``artcraft_tasks`` 행(범주·앱·옵션)이고, 라운드는 그 세션의
Run 이다. 실행 직전 ``prepare_run`` 이 작업 폴더를 만들고 앱별 MCP 서버 명세·지시문 머리말·시스템 노트를 돌려주면 manager 가 기존 경로로
엔진을 띄운다. 끝나면 ``sweep_outputs`` 가 ``out/`` 의 결과를 이 Run 의 outbox 로 복사해 기존 ``_collect_artifacts`` 가 Artifact 로 등록한다.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari import artcraft
from ttakkari import artcraft_catalog as catalog
from ttakkari.artifacts.service import content_path
from ttakkari.broker.policy import BrokerError, validate_workspace_root
from ttakkari.config import get_settings
from ttakkari.models import (
    ArtcraftTask,
    Artifact,
    ChatSession,
    EngineAccount,
    ExportPolicy,
    Run,
    RunEvent,
    RunStatus,
    SessionKind,
    Workspace,
    utcnow,
)
from ttakkari.runs import profiles
from ttakkari.schemas import ArtifactOut

WORKSPACE_NAME = "ArtCraft 작업"
OUTPUT_SOURCE = "agent_output"
ACTIVE = (RunStatus.queued, RunStatus.running)
PREV_LIMIT = 8
RESULT_CHARS = 4000
MESSAGES_KEEP = 3
MESSAGE_CHARS = 1500
TITLE_CHARS = 60
STATE_OF_RUN = {
    RunStatus.queued: "queued", RunStatus.running: "running", RunStatus.succeeded: "done", RunStatus.failed: "failed",
    RunStatus.cancelled: "cancelled", RunStatus.interrupted: "interrupted",
}


def _err(status: int, code: str, message: str) -> artcraft.ArtcraftError:
    return artcraft.ArtcraftError(status, code, message)


# ---------------------------------------------------------------- 워크스페이스·작업 폴더

async def ensure_workspace(db: AsyncSession) -> Workspace:
    s = get_settings()
    root = s.artcraft_workspace_root.expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
        real = validate_workspace_root(str(root), s.allowed_roots)
    except (OSError, BrokerError) as exc:
        raise _err(503, "workspace_unavailable", f"ArtCraft 작업 폴더를 쓸 수 없습니다: {exc}") from exc
    ws = await db.scalar(select(Workspace).where(Workspace.root_path == str(real)))
    if ws is None:
        ws = Workspace(name=WORKSPACE_NAME, root_path=str(real), is_git=(real / ".git").exists(), default_engine="claude")
        db.add(ws)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            ws = await db.scalar(select(Workspace).where(Workspace.root_path == str(real)))
            assert ws is not None
    elif ws.archived_at is not None:
        ws.archived_at = None
        await db.commit()
    return ws


def task_dir(ws: Workspace, task_id: uuid.UUID) -> Path:
    return Path(ws.root_path) / str(task_id)


# ---------------------------------------------------------------- 입력 검증

def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _category(category_id: str) -> catalog.Category:
    cat = catalog.category(category_id)
    if cat is None:
        raise _err(404, "unknown_category", "알 수 없는 작업 범주입니다.")
    return cat


def clean_options(tpl: catalog.Template | None, options: dict[str, str] | None) -> dict[str, str]:
    """템플릿이 정의한 옵션만, 선택지에 있는 값만 남긴다. 없는 옵션은 기본값."""
    if tpl is None:
        return {}
    given = options or {}
    out: dict[str, str] = {}
    for opt in tpl.options:
        value = given.get(opt.id, opt.default)
        if value not in {v for v, _ in opt.choices}:
            raise _err(422, "bad_input", f"'{opt.label}' 값이 올바르지 않습니다.")
        out[opt.id] = value
    return out


async def _load_inputs(db: AsyncSession, cat: catalog.Category, ids: list[uuid.UUID], min_files: int = 0) -> list[Artifact]:
    unique = list(dict.fromkeys(ids))
    if len(unique) > catalog.FILES_MAX:
        raise _err(422, "bad_input", f"파일은 한 번에 {catalog.FILES_MAX}개까지 붙일 수 있어요.")
    found = {a.id: a for a in await db.scalars(select(Artifact).where(Artifact.id.in_(unique)))} if unique else {}
    arts: list[Artifact] = []
    for aid in unique:
        art = found.get(aid)
        if art is None or art.deleted_at is not None:
            raise _err(422, "bad_input", "붙인 파일을 찾을 수 없어요. 다시 올려 주세요.")
        if art.export_policy == ExportPolicy.sensitive:
            raise _err(422, "bad_input", f"'{art.filename}'은 민감 파일로 분류돼 보낼 수 없어요.")
        if _ext(art.filename) not in cat.accepts:
            accepted = ", ".join(sorted(cat.accepts)[:14])
            raise _err(422, "bad_input", f"'{art.filename}'은 {cat.title}에서 받지 않는 형식이에요. 받는 형식: {accepted}")
        arts.append(art)
    if len(arts) < min_files:
        raise _err(422, "bad_input", f"이 작업에는 파일이 {min_files}개 이상 필요해요.")
    return arts


def _title(prompt: str, tpl: catalog.Template | None) -> str:
    if tpl is not None:
        return tpl.title
    line = prompt.strip().splitlines()[0] if prompt.strip() else ""
    return (line[:TITLE_CHARS] or "ArtCraft 작업")


def selected_apps(category_id: str, template_id: str | None, apps: list[str] | None) -> list[str]:
    """Resolve and validate a task's app set before its admission lock is acquired."""
    cat = _category(category_id)
    tpl = catalog.template(cat, template_id)
    if template_id and tpl is None:
        raise _err(422, "bad_input", "알 수 없는 템플릿입니다.")
    chosen = list(dict.fromkeys(apps)) if apps else list(tpl.apps if tpl and tpl.apps else catalog.app_ids(cat))
    if not chosen or any(a not in catalog.app_ids(cat) for a in chosen):
        raise _err(422, "unknown_app", "이 범주에서 쓸 수 없는 앱이 포함돼 있어요.")
    return chosen


# ---------------------------------------------------------------- 작업·업데이트 상호 배타성 계약

def check_app_not_updating(apps: list[str]) -> None:
    """해당 앱이 현재 업데이트/교체 중인지 검사하여 동시 실행 충돌을 방지한다."""
    from ttakkari import artcraft

    try:
        updates = artcraft.load_state_strict().get("updates") or {}
        if not isinstance(updates, dict):
            raise ValueError("ArtCraft updates state must be an object")  # noqa: TRY004
    except Exception as exc:
        raise _err(503, "busy_check_unavailable", f"업데이트 상태를 확인할 수 없습니다: {exc}") from exc

    for app in apps:
        entry = updates.get(app)
        if isinstance(entry, dict) and entry.get("state") in ("queued", "running"):
            raise _err(409, "app_updating", f"'{app}' 앱 업데이트/교체가 진행 중입니다. 잠시 후 다시 시도해 주세요.")

    jobs = getattr(artcraft, "JOBS", {})
    if not isinstance(jobs, dict):
        return
    for job in jobs.values():
        if not getattr(job, "active", False):
            continue
        if getattr(job, "kind", None) in ("update", "rollback"):
            job_apps = getattr(job, "apps", [])
            for app in apps:
                if app in job_apps:
                    raise _err(409, "app_updating", f"'{app}' 앱 업데이트/교체가 진행 중입니다. 잠시 후 다시 시도해 주세요.")


async def active_task_apps(db: AsyncSession) -> set[str]:
    """현재 대기(queued) 또는 실행(running) 중인 제작 작업이 사용 중인 앱 ID 집합.

    업데이트 백엔드(artcraft_update.check_busy)가 앱 교체 전 활성 제작 작업과의 충돌을 검사할 때 호출한다.
    """
    active_session_ids = select(Run.session_id).where(Run.status.in_(ACTIVE))
    tasks = list(await db.scalars(select(ArtcraftTask).where(ArtcraftTask.session_id.in_(active_session_ids))))
    busy: set[str] = set()
    for t in tasks:
        busy.update(t.apps or [])
    return busy



# ---------------------------------------------------------------- 실행 엔진 동적 결정

async def resolve_task_engine(db: AsyncSession) -> tuple[str, str]:
    """ArtCraft 작업 실행에 사용할 준비된 개인 계정 엔진을 동적으로 선택한다.

    개인 계정의 유효 순서(임시 우선순위 포함)에서 첫 실행 가능 계정을 선택한다.
    계정이 꺼져 있으면 건너뛰고, 실행 가능한 계정이 없으면 명확한 503 오류를 발생시킨다.
    """
    accounts = await profiles.effective_order(db, pool="personal")
    active = [a for a in accounts if profiles.is_on(a, pool="personal")]
    if not active:
        raise _err(503, "no_active_engine", "활성화된 실행 엔진(계정)이 없습니다. 설정에서 엔진을 켜주세요.")
    available_by_engine: dict[str, set[uuid.UUID]] = {}
    for account in active:
        if account.engine not in available_by_engine:
            ready, _blocked = await profiles.availability(db, account.engine, pool="personal")
            available_by_engine[account.engine] = {profile.id for profile in ready}
        if account.id in available_by_engine[account.engine]:
            chosen = account
            break
    else:
        raise _err(503, "no_ready_engine", "실행 준비된 엔진(계정)이 없습니다. 계정 준비 상태와 한도/쿨다운을 확인해 주세요.")
    return chosen.engine, f"{chosen.engine.capitalize()} ({chosen.name})"


async def resolve_task_account(db: AsyncSession, account_id: uuid.UUID) -> EngineAccount:
    """Validate an explicitly requested ArtCraft account without considering alternatives."""
    account = await db.get(EngineAccount, account_id)
    if account is None or not profiles.is_on(account, pool="personal"):
        raise _err(503, "account_unavailable", "지정한 계정을 개인 pool에서 찾을 수 없거나 비활성 상태입니다.")
    free, blocked = await profiles.availability(db, account.engine, pool="personal")
    selected = next((p for p in free if p.id == account_id), None)
    waiting = next((until for p, until in blocked if p.id == account_id), None)
    if selected is None:
        if waiting is not None:
            reason = "사용량 한도 또는 쿨다운 중"
        else:
            reason = "인증 또는 실행 준비가 되지 않음"
        raise _err(503, "account_unavailable", f"지정한 계정을 사용할 수 없습니다({reason}). 다른 계정으로 대체하지 않았습니다.")
    if profiles.cooldown_until(profiles.model_capacity_key(selected.engine, selected.model)):
        raise _err(503, "account_unavailable", "지정한 계정의 모델이 한도 중입니다. 다른 계정으로 대체하지 않았습니다.")
    return account


# ---------------------------------------------------------------- 작업 생성·라운드

async def _queue(db: AsyncSession, sess: ChatSession, task: ArtcraftTask, prompt: str, input_ids: list[uuid.UUID],
                 options: dict[str, str], speed: str, use_previous: bool, ip: str | None) -> Run:
    from ttakkari.api.sessions import queue_run

    def tag(run: Run) -> None:
        run.usage = {
            **(run.usage or {}),
            "artcraft": {
                "task_id": str(task.id),
                "options": options,
                "use_previous": use_previous,
                "engine": sess.engine,
                "account_id": str(sess.account_id) if sess.account_id else None,
            },
        }

    return await queue_run(db, sess, prompt, context_artifact_ids=input_ids, effort=catalog.SPEEDS.get(speed), ip=ip,
                           on_queued=tag)


async def create_task(db: AsyncSession, *, category_id: str, prompt: str, template_id: str | None,
                      input_ids: list[uuid.UUID], apps: list[str] | None, options: dict[str, str] | None, speed: str,
                      ip: str | None, account_id: uuid.UUID | None = None) -> tuple[ArtcraftTask, Run]:
    cat = _category(category_id)
    tpl = catalog.template(cat, template_id)
    chosen = selected_apps(category_id, template_id, apps)
    check_app_not_updating(chosen)
    clean = clean_options(tpl, options)
    await _load_inputs(db, cat, input_ids, tpl.min_files if tpl else 0)
    if account_id is None:
        engine, _engine_desc = await resolve_task_engine(db)
    else:
        account = await resolve_task_account(db, account_id)
        engine = account.engine
    ws = await ensure_workspace(db)
    sess = ChatSession(workspace_id=ws.id, title=_title(prompt, tpl), engine=engine, kind=SessionKind.chat,
                       pool="personal", account_id=account_id)
    db.add(sess)
    await db.flush()
    task = ArtcraftTask(session_id=sess.id, category=cat.id, apps=chosen, title=sess.title, template_id=tpl.id if tpl else None,
                        options=clean)
    db.add(task)
    await db.flush()
    run = await _queue(db, sess, task, prompt, input_ids, clean, speed, False, ip)
    await db.refresh(task)
    return task, run


async def get_task(db: AsyncSession, task_id: uuid.UUID) -> ArtcraftTask:
    task = await db.get(ArtcraftTask, task_id)
    if task is None:
        raise _err(404, "not_found", "작업을 찾을 수 없습니다.")
    return task


async def add_round(db: AsyncSession, task: ArtcraftTask, *, prompt: str, input_ids: list[uuid.UUID], use_previous: bool,
                    options: dict[str, str] | None, speed: str, ip: str | None,
                    account_id: uuid.UUID | None = None) -> Run:
    if task.archived_at is not None:
        raise _err(409, "archived", "보관한 작업에는 이어서 지시할 수 없어요.")
    check_app_not_updating(list(task.apps or []))
    cat = _category(task.category)
    await _load_inputs(db, cat, input_ids)
    sess = await db.get(ChatSession, task.session_id)
    if sess is None:
        raise _err(404, "not_found", "작업 세션을 찾을 수 없습니다.")
    if account_id is not None and account_id != sess.account_id:
        raise _err(409, "account_pin_immutable", "작업 생성 뒤에는 계정을 새로 지정하거나 변경할 수 없습니다.")
    tpl = catalog.template(cat, task.template_id)
    clean = clean_options(tpl, options) if options else dict(task.options or {})
    task.updated_at = utcnow()
    return await _queue(db, sess, task, prompt, input_ids, clean, speed, use_previous, ip)


# ---------------------------------------------------------------- 조회

def _artifact(a: Artifact) -> dict[str, Any]:
    return ArtifactOut.from_model(a).model_dump(mode="json")


def _state(run: Run | None) -> str:
    return STATE_OF_RUN.get(run.status, "queued") if run is not None else "queued"


def _expects_output_file(task: ArtcraftTask, run: Run | None = None) -> bool:
    """정보·텍스트 조회는 파일을 만들지 않아도 성공이며, 나머지 요청은 기본적으로 파일을 만든다."""
    if task.template_id in {"info", "text", "dxf-info"}:
        return False
    if run is not None:
        prompt = (run.prompt or "").lower()
        lookup = ("알려줘", "알려 주세요", "몇 개", "무엇", "무슨", "정보", "읽어줘", "읽어 주세요", "요약해 주세요", "요약해줘", "요약해 주", "요약해", "text ", "info ",
                  "what is", "what are", "what's", "who is", "where is", "when is", "why ",
                  "how many", "how much", "how long", "how tall", "how wide", "tell me", "show me",
                  "describe", "explain", "identify", "summarize", "read ")
        without_create = re.sub(
            r"\bwithout\s+(?:(?:editing|modifying)\s+or\s+)?(?:creating|create|making|saving|editing|modifying|exporting)\b"
            r"|\b(?:do\s+not|don't|never)\s+(?:create|make|save|edit|modify|export|render|generate|design|produce|convert)"
            r"(?:(?:\s*,\s*(?:or\s+)?|\s+or\s+)(?:create|make|save|edit|modify|export|render|generate|design|produce|convert))*\b"
            r"|(?:저장하|만들|제작하|내보내|편집하|수정하?)지\s+(?:말고|마세요|마십시오|말아)",
            " ", prompt,
        )
        create = re.search(
            r"(?:저장|만들어|제작|내보내)"
            r"|(?:편집|수정)(?:하(?:고|며|면서|여|여서|세요|십시오|면|려면|도록|자|겠습니다|였(?:고|다|습니다)?)"
            r"|해(?:서|도|요|주세요| 주세요|줘| 줘|야|라)?|한\s+(?:뒤|다음|후))"
            r"|\b(?:export|render|create|creating|make|making|draw|generate|design|produce|save|edit|modify)\b",
            without_create,
        )
        if any(word in prompt for word in lookup) and create is None:
            return False
    return True


async def _runs_by_session(db: AsyncSession, session_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Run]]:
    out: dict[uuid.UUID, list[Run]] = {sid: [] for sid in session_ids}
    if not session_ids:
        return out
    for run in await db.scalars(select(Run).where(Run.session_id.in_(session_ids)).order_by(Run.created_at)):
        out[run.session_id].append(run)
    return out


async def summaries(db: AsyncSession, tasks: list[ArtcraftTask]) -> list[dict[str, Any]]:
    sids = [t.session_id for t in tasks]
    runs = await _runs_by_session(db, sids)
    counts: dict[uuid.UUID, int] = {}
    latest_counts: dict[uuid.UUID, int] = {}
    if sids:
        latest_runs = {rs[-1].id: sid for sid, rs in runs.items() if rs}
        rows = await db.execute(
            select(Artifact.session_id, Artifact.run_id, func.count()).where(
                Artifact.session_id.in_(sids), Artifact.source == OUTPUT_SOURCE, Artifact.deleted_at.is_(None),
            ).group_by(Artifact.session_id, Artifact.run_id))
        for sid, run_id, n in rows.all():
            counts[sid] = counts.get(sid, 0) + n
            if run_id in latest_runs:
                latest_counts[sid] = latest_counts.get(sid, 0) + n
    from ttakkari.api import desk_feedback
    last_runs = [rs[-1] for rs in runs.values() if rs]
    detailed = {r.id: r for r in await desk_feedback.runs_out(db, [r for r in last_runs if r.status in ACTIVE])}
    items = []
    for t in tasks:
        rs = runs.get(t.session_id, [])
        last = rs[-1] if rs else None
        live = detailed.get(last.id) if last is not None else None
        output_count = counts.get(t.session_id, 0)
        last_error = (last.error or "")[:300] if last is not None and last.status == RunStatus.failed else None
        if (last is not None and last.status == RunStatus.succeeded and _expects_output_file(t, last)
                and latest_counts.get(t.session_id, 0) == 0 and not last_error):
            last_error = "결과 파일 없음 (산출물이 생성되지 않았습니다)"
        items.append({
            "id": str(t.id), "session_id": str(t.session_id), "category": t.category, "title": t.title, "apps": list(t.apps or []),
            "template_id": t.template_id, "state": _state(last), "round_count": len(rs), "output_count": output_count,
            "last_run_id": str(last.id) if last else None,
            "error": last_error,
            "progress": live.last_progress if live else None, "waiting_reason": live.waiting_reason if live else None,
            "created_at": t.created_at, "updated_at": t.updated_at, "archived_at": t.archived_at,
        })
    return items


async def list_tasks(db: AsyncSession, *, category_id: str | None, state: str, limit: int,
                     session_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    q = select(ArtcraftTask)
    if session_id is not None:
        # 결과물에서 작업을 찾는 조회다. 보관한 작업도 이어서 볼 수 있어야 해서 보관 여부는 거르지 않는다.
        q = q.where(ArtcraftTask.session_id == session_id)
    else:
        q = q.where(ArtcraftTask.archived_at.is_(None))
    if category_id:
        q = q.where(ArtcraftTask.category == category_id)
    if state == "active":
        q = q.where(ArtcraftTask.session_id.in_(select(Run.session_id).where(Run.status.in_(ACTIVE))))
    tasks = list(await db.scalars(q.order_by(ArtcraftTask.updated_at.desc()).limit(limit)))
    return await summaries(db, tasks)


async def task_detail(db: AsyncSession, task: ArtcraftTask) -> dict[str, Any]:
    from ttakkari.api import desk_feedback
    runs = (await _runs_by_session(db, [task.session_id]))[task.session_id]
    detailed = {r.id: r for r in await desk_feedback.runs_out(db, runs)}
    outputs: dict[uuid.UUID, list[Artifact]] = {}
    for art in await db.scalars(select(Artifact).where(
            Artifact.session_id == task.session_id, Artifact.source == OUTPUT_SOURCE, Artifact.deleted_at.is_(None),
    ).order_by(Artifact.created_at, Artifact.filename)):
        outputs.setdefault(art.run_id, []).append(art)  # type: ignore[arg-type]
    # 사용자 전역 Stop 훅 같은 후처리가 마지막 답을 딴 이야기로 바꿀 수 있어, 라운드마다 마지막 답 몇 개를 함께 준다.
    messages: dict[uuid.UUID, list[str]] = {}
    if runs:
        for ev in await db.scalars(select(RunEvent).where(RunEvent.run_id.in_([r.id for r in runs]), RunEvent.type == "message")
                                   .order_by(RunEvent.run_id, RunEvent.seq)):
            text = str((ev.payload or {}).get("text") or "").strip()
            if text:
                messages.setdefault(ev.run_id, []).append(text[:MESSAGE_CHARS])
    wanted = {uuid.UUID(str(i)) for r in runs for i in r.context_artifact_ids or []}
    inputs = {a.id: a for a in await db.scalars(select(Artifact).where(Artifact.id.in_(wanted)))} if wanted else {}
    rounds = []
    for index, run in enumerate(runs, 1):
        meta = (run.usage or {}).get("artcraft") or {}
        live = detailed.get(run.id)
        round_outputs = outputs.get(run.id, [])
        round_error = run.error
        if (run.status == RunStatus.succeeded and _expects_output_file(task, run)
                and len(round_outputs) == 0 and not round_error):
            round_error = "결과 파일 없음 (산출물이 생성되지 않았습니다)"
        rounds.append({
            "id": str(run.id), "index": index, "status": run.status, "state": _state(run), "prompt": run.prompt,
            "result_text": (run.result_text or "")[:RESULT_CHARS] or None,
            "messages": messages.get(run.id, [])[-MESSAGES_KEEP:], "error": round_error,
            "options": meta.get("options") or {}, "use_previous": bool(meta.get("use_previous")),
            "inputs": [_artifact(inputs[uuid.UUID(str(i))]) for i in run.context_artifact_ids or [] if uuid.UUID(str(i)) in inputs],
            "outputs": [_artifact(a) for a in round_outputs],
            "progress": live.last_progress if live else None, "waiting_reason": live.waiting_reason if live else None,
            "created_at": run.created_at, "started_at": run.started_at, "finished_at": run.finished_at,
        })
    summary = (await summaries(db, [task]))[0]
    return {**summary, "options": dict(task.options or {}), "rounds": rounds}


async def outputs_gallery(db: AsyncSession, *, category_id: str | None, kind: str | None, q: str | None, limit: int,
                          offset: int) -> dict[str, Any]:
    query = (select(Artifact, ArtcraftTask)
             .join(ArtcraftTask, ArtcraftTask.session_id == Artifact.session_id)
             .where(Artifact.source == OUTPUT_SOURCE, Artifact.deleted_at.is_(None)))
    if category_id:
        query = query.where(ArtcraftTask.category == category_id)
    if kind:
        query = query.where(Artifact.kind == kind)
    if q:
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(Artifact.filename.ilike(f"%{escaped}%", escape="\\"))
    rows = (await db.execute(query.order_by(Artifact.created_at.desc(), Artifact.id).offset(offset).limit(limit + 1))).all()
    items = [{"artifact": _artifact(a), "task_id": str(t.id), "task_title": t.title, "category": t.category,
              "run_id": str(a.run_id) if a.run_id else None} for a, t in rows[:limit]]
    return {"items": items, "has_more": len(rows) > limit}


# ---------------------------------------------------------------- 실행 준비(manager 가 부른다)

@dataclass
class RunPlan:
    prompt: str
    note: str
    mcp_servers: dict[str, dict[str, Any]]
    out_dir: Path
    log: list[str] = field(default_factory=list)


def mcp_spec(role: catalog.AppRole, workdir: Path) -> dict[str, Any]:
    """앱 하나의 stdio MCP 서버 명세. 인자는 registry 앱 이름과 고정 옵션·작업 폴더 경로뿐이고 셸을 거치지 않는다."""
    args = ["mcp", artcraft.known_app(role.app)]
    folder = str(workdir)
    if role.confine == "rootflags":
        args += ["--automation-read-root", folder, "--automation-write-root", folder]
    elif role.confine == "root":
        args += ["--root", folder]
    if role.compact:
        args.append("--compact")
    return {"command": str(artcraft.binary_path()), "args": args, "env": {}}


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _stage(art: Artifact, folder: Path) -> Path | None:
    try:
        dest = folder / Path(art.filename).name
        _copy(content_path(art), dest)
        return dest
    except (BrokerError, OSError):
        return None


async def _round_index(db: AsyncSession, run: Run) -> int:
    n = await db.scalar(select(func.count()).select_from(Run).where(Run.session_id == run.session_id, Run.created_at <= run.created_at))
    return int(n or 1)


async def _previous_outputs(db: AsyncSession, run: Run) -> list[Artifact]:
    """이 Run 보다 앞선 Run 중 결과가 있는 가장 최근 라운드의 결과 파일(성공한 런 우선)."""
    prior = list(await db.scalars(select(Run).where(Run.session_id == run.session_id, Run.created_at < run.created_at)
                                  .order_by(Run.created_at.desc())))
    # 성공한 Run 의 결과물 우선
    for earlier in prior:
        if earlier.status == RunStatus.succeeded:
            arts = list(await db.scalars(select(Artifact).where(
                Artifact.run_id == earlier.id, Artifact.source == OUTPUT_SOURCE, Artifact.deleted_at.is_(None),
                Artifact.export_policy != ExportPolicy.sensitive).order_by(Artifact.filename).limit(PREV_LIMIT)))
            if arts:
                return arts
    # 성공한 런이 없거나 결과가 없으면 전체 앞선 런 중 탐색
    for earlier in prior:
        arts = list(await db.scalars(select(Artifact).where(
            Artifact.run_id == earlier.id, Artifact.source == OUTPUT_SOURCE, Artifact.deleted_at.is_(None),
            Artifact.export_policy != ExportPolicy.sensitive).order_by(Artifact.filename).limit(PREV_LIMIT)))
        if arts:
            return arts
    return []


NOTE = """\
[ArtCraft 작업 지침] 범주: {category}. 연결된 앱 MCP 서버: {servers}.
- 작업 폴더: {base}  (in/ 입력 복사본, prev/ 이전 결과 복사본, work/ 중간 파일, out/ 최종 결과). 앱 도구는 이 폴더 안의 파일만 읽고 쓰게 설정돼 있다.
- 입력 파일(in/, prev/)은 고치지 말고 읽기만 한다. 고칠 때는 work/ 로 복사해 작업하고, 최종 결과는 반드시 {out} 아래에 의미 있는 이름(예: report-v2.docx)으로 저장한다. work/ 나 작업 폴더 루트에만 둔 파일은 자동 수집되지 않는다. {out} 아래에 저장한 결과는 자동 수집되므로 결과 파일에 share_file 을 호출하지 않는다(자동 수집과 중복 등록될 수 있음). share_file 은 대화에 파일을 공유할 때 쓰고, keep_artifact 는 사용자가 요청했을 때 보관함에 넣는 도구다.
- 앱 MCP 도구가 안 보이거나 오류가 나면 즉시 CLI 로 같은 일을 한다: {launcher} run <app> ... (명령 ID 목록은 `{launcher} run <app> commands`, 상세는 {agents}).
- CLI 명령어 호출 시 shell 환경변수나 명령어 치환($VAR, $(cmd), eval)을 쓰지 않고 전체 리터럴 인자를 직접 실행한다. sudo, bypass 등 하네스 보안 가드를 우회하지 않는다.
- 끝내기 전에 결과가 0바이트가 아닌지 확인하고 렌더·info·text 등으로 직접 열어 본다(이미지는 PNG 로 렌더). 확인한 내용을 답에 한두 줄로 적고, 요구한 결과 파일을 만들지 못했으면 실패 이유를 솔직히 적는다.
- 앱이 못 하는 일은 지어내지 말고 무엇이 안 됐는지 솔직히 알린다. 유료·클라우드·AI 제공자 기능은 쓰지 않는다. 비밀·토큰 파일은 읽지 않는다.
- 사용자는 폰으로 본다. 답은 한국어로 짧게(만든 파일 이름과 한 줄 설명, 막힌 점).
{recipes}"""


async def prepare_run(db: AsyncSession, sess: ChatSession, run: Run, outbox: Path) -> RunPlan | None:
    task = await db.scalar(select(ArtcraftTask).where(ArtcraftTask.session_id == sess.id))
    if task is None:
        return None
    ws = await db.get(Workspace, sess.workspace_id)
    cat = catalog.category(task.category)
    if ws is None or cat is None:
        return None
    base = task_dir(ws, task.id)
    folders = {name: base / name for name in ("in", "prev", "work", "out")}
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)
    meta = (run.usage or {}).get("artcraft") or {}
    index = await _round_index(db, run)
    log: list[str] = []

    inputs: list[Path] = []
    for aid in run.context_artifact_ids or []:
        art = await db.get(Artifact, uuid.UUID(str(aid)))
        if art is None or art.deleted_at is not None:
            continue
        staged = _stage(art, folders["in"] / f"r{index}")
        if staged is None:
            log.append(f"첨부 파일을 작업 폴더로 복사하지 못했습니다: {art.filename}")
        else:
            inputs.append(staged)
    previous: list[Path] = []
    if meta.get("use_previous"):
        for art in await _previous_outputs(db, run):
            staged = _stage(art, folders["prev"] / f"r{index}")
            if staged is not None:
                previous.append(staged)
            else:
                log.append(f"이전 결과 파일을 작업 폴더로 복사하지 못했습니다: {art.filename}")

    servers: dict[str, dict[str, Any]] = {}
    roles = {a.app: a for a in cat.apps}
    for app in task.apps or []:
        role = roles.get(app)
        if role is None:
            continue
        try:
            servers[f"artcraft_{app}"] = mcp_spec(role, base)
        except artcraft.ArtcraftError as exc:
            log.append(f"{role.title} MCP 서버를 연결하지 못했습니다: {exc.message}")

    def rel(p: Path) -> str:
        return f"{p.relative_to(base).as_posix()}  ({p})"

    options = meta.get("options") or {}
    tpl = catalog.template(cat, task.template_id)
    option_text = ", ".join(f"{o.label}={dict(o.choices).get(options.get(o.id, o.default), options.get(o.id, o.default))}"
                            for o in (tpl.options if tpl else ())) or "없음"
    head = [f"[ArtCraft 작업 · {cat.title}] 라운드 {index}", f"작업 폴더: {base}"]
    head.append("입력 파일(고치지 말고 읽기만): " + (", ".join(rel(p) for p in inputs) if inputs else "없음"))
    if meta.get("use_previous"):
        head.append("이전 결과 파일(이어서 고칠 대상): " + (", ".join(rel(p) for p in previous) if previous else "없음(앞 라운드에 결과 파일이 없었음)"))
    head.append(f"선택 옵션: {option_text}")
    head.append(f"최종 결과 저장 위치: {folders['out']} (반드시 이 위치에 저장)")
    if task.category == "motion" or (task.apps and "effectcraft" in task.apps):
        head.append(
            "[EffectCraft 동영상 렌더 안내] EffectCraft MCP 도구는 단일 프레임 렌더(render_frame)만 지원합니다. "
            "동영상(.mp4)을 생성하려면 프로젝트를 저장한 뒤 CLI를 실행하세요: "
            f"`artcraft run effectcraft --project project.ecproj render --out {folders['out']}/effectcraft-render.mp4`"
        )
    if log:
        head.append("주의: " + " / ".join(log))
    prompt = "\n".join(head) + "\n\n[사용자 지시]\n" + run.prompt

    launcher = str(artcraft.binary_path()) if artcraft.binary_ok() else "<artcraft 저장소>/bin/artcraft"
    agents_md = str(artcraft.root() / "AGENTS.md")
    recipes = "\n".join(f"  · {line.replace('artcraft ', launcher + ' ')}" if "artcraft" in line else f"  · {line}" for line in cat.cli)
    note = NOTE.format(category=cat.title, servers=", ".join(servers) or "없음(CLI 로 진행)", base=base, out=folders["out"],
                       launcher=launcher, agents=agents_md, recipes="앱별 CLI 요령:\n" + recipes if recipes else "")
    return RunPlan(prompt=prompt, note=note, mcp_servers=servers, out_dir=folders["out"], log=log)


SENSITIVE_FILENAMES = {".env", ".git", ".DS_Store", "desktop.ini", "Thumbs.db"}


def sweep_outputs(plan: RunPlan, run: Run, outbox: Path) -> int:
    """out/ 에 이 Run 이 새로 만든 유효한 파일을 outbox 로 복사한다.

    - 0바이트 빈 파일과 숨김/민감 파일(.env 등)은 결과물로 수집하지 않는다.
    - 심볼릭 링크 및 작업 폴더 외부를 가리키는 경로는 엄격히 배제한다.
    - 이전 시도와 크기가 같아도 내용이 수정되었거나 mtime이 새로우면 정상 복사한다.
    - 이미 같은 Run outbox 또는 out/ 에 같은 파일명·내용이 있으면 다시 복사하지 않는다."""
    start_time = run.started_at or run.created_at
    since = start_time.timestamp() if start_time else 0.0
    copied = 0
    root = plan.out_dir
    if not root.is_dir():
        return 0
    resolved_root = root.resolve()
    existing: set[tuple[str, str]] = set()
    for candidate in outbox.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            continue
        existing.add((candidate.name, digest))
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        # 숨김 파일 및 민감 파일 배제
        if path.name.startswith(".") or path.name in SENSITIVE_FILENAMES:
            continue
        try:
            resolved_path = path.resolve()
            # 심볼릭 링크 디렉토리 등으로 root 바깥을 가리키는 경로 차단
            if not resolved_path.is_relative_to(resolved_root):
                continue
            stat = path.stat()
            # 0바이트 빈 파일은 결과물로 인정하지 않는다(에이전트 실패 은폐 방지)
            if stat.st_size == 0:
                continue
            dest = outbox / path.relative_to(root)
            # mtime 검사: since 이전 생성된 파일은 건너뜀
            if since > 0 and stat.st_mtime < since:
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            identity = (path.name, digest)
            if identity in existing:
                continue
            # dest 가 이미 존재하고, 내용이 완전히 동일(크기와 내용 일치)하면 건너뜀
            if dest.exists():
                dstat = dest.stat()
                if dstat.st_size == stat.st_size and dstat.st_mtime >= stat.st_mtime:
                    try:
                        if path.read_bytes() == dest.read_bytes():
                            continue
                    except OSError:
                        pass
            _copy(path, dest)
            existing.add(identity)
            copied += 1
        except OSError:
            continue
    return copied


# ---------------------------------------------------------------- 산출물 독립 검증

def verify_task_output(file_path: Path, expected_kind: str | None = None) -> dict[str, Any]:
    """제작 산출물의 무결성(0바이트 빈 파일, 헤더 손상, 매직 바이트, 확장자 일치)을 독립 검증한다."""
    p = Path(file_path)
    if not p.exists():
        return {"valid": False, "error": "파일이 존재하지 않습니다", "kind": None, "size_bytes": 0}
    if p.is_symlink():
        return {"valid": False, "error": "심볼릭 링크는 허용되지 않습니다", "kind": None, "size_bytes": 0}
    try:
        stat = p.stat()
    except OSError as exc:
        return {"valid": False, "error": f"파일 상태를 읽을 수 없습니다: {exc}", "kind": None, "size_bytes": 0}
    if stat.st_size == 0:
        return {"valid": False, "error": "0바이트 빈 파일입니다 (결과물 없음)", "kind": None, "size_bytes": 0}

    kind: str | None = None
    try:
        header = p.read_bytes()[:64]
    except OSError as exc:
        return {"valid": False, "error": f"파일을 읽을 수 없습니다: {exc}", "kind": None, "size_bytes": stat.st_size}

    # 바이너리 매직 바이트 검사
    if header.startswith(b"%PDF"):
        kind = "pdf"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    elif header.startswith(b"\xff\xd8\xff"):
        kind = "jpg"
    elif header.startswith((b"GIF87a", b"GIF89a")):
        kind = "gif"
    elif len(header) >= 8 and (header[4:8] == b"ftyp" or b"ftyp" in header[:32]):
        kind = "mp4"
    elif header.startswith(b"PK\x03\x04"):
        ext = p.suffix.lower().lstrip(".")
        kind = ext if ext in {"docx", "xlsx", "pptx", "zip"} else "zip"
    elif header.startswith(b"\x1a\x45\xdf\xa3"):
        kind = "webm"
    elif header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"WAVE":
        kind = "wav"
    else:
        # 텍스트 계열 포맷 검사 (SVG, DXF, JSON 등)
        try:
            head_txt = p.read_text(encoding="utf-8", errors="replace")[:1000].strip()
            if "<svg" in head_txt.lower():
                kind = "svg"
            elif "section" in head_txt.lower() and any(k in head_txt.lower() for k in ("header", "entities", "0\n", "0\r\n")):
                kind = "dxf"
            elif head_txt.startswith(("{", "[")):
                try:
                    import json
                    json.loads(p.read_text(encoding="utf-8"))
                    ext = p.suffix.lower().lstrip(".")
                    kind = ext if ext in {"json", "ecproj"} else "json"
                except Exception:  # noqa: BLE001, S110 - Ignore unrecognized optional file headers.
                    pass
        except Exception:  # noqa: BLE001, S110 - Ignore unreadable optional file headers.
            pass

    if kind is None:
        return {"valid": False, "error": f"지원하지 않거나 인식할 수 없는 파일 포맷 헤더입니다: {header[:16]!r}", "kind": None, "size_bytes": stat.st_size}

    if expected_kind is not None and kind != expected_kind.lower():
        return {
            "valid": False,
            "error": f"기대한 형식({expected_kind})과 실제 감지된 형식({kind})이 일치하지 않습니다",
            "kind": kind,
            "size_bytes": stat.st_size,
        }

    return {"valid": True, "error": None, "kind": kind, "size_bytes": stat.st_size}
