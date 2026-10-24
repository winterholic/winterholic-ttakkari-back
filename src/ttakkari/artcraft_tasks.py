"""ArtCraft 작업(ADR 0039): 사용자의 창작 지시를 기존 세션·Run·Artifact 위에서 돌린다.

새 실행기는 없다. 작업 = ``sessions`` 행 하나(``kind=chat``, 엔진 claude) + ``artcraft_tasks`` 행(범주·앱·옵션)이고, 라운드는 그 세션의
Run 이다. 실행 직전 ``prepare_run`` 이 작업 폴더를 만들고 앱별 MCP 서버 명세·지시문 머리말·시스템 노트를 돌려주면 manager 가 기존 경로로
엔진을 띄운다. 끝나면 ``sweep_outputs`` 가 ``out/`` 의 결과를 이 Run 의 outbox 로 복사해 기존 ``_collect_artifacts`` 가 Artifact 로 등록한다.
"""
from __future__ import annotations

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
    ExportPolicy,
    Run,
    RunEvent,
    RunStatus,
    SessionKind,
    Workspace,
    utcnow,
)
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


# ---------------------------------------------------------------- 작업 생성·라운드

async def _queue(db: AsyncSession, sess: ChatSession, task: ArtcraftTask, prompt: str, input_ids: list[uuid.UUID],
                 options: dict[str, str], speed: str, use_previous: bool, ip: str | None) -> Run:
    from ttakkari.api.sessions import queue_run

    def tag(run: Run) -> None:
        run.usage = {**(run.usage or {}), "artcraft": {"task_id": str(task.id), "options": options, "use_previous": use_previous}}

    return await queue_run(db, sess, prompt, context_artifact_ids=input_ids, effort=catalog.SPEEDS.get(speed), ip=ip,
                           on_queued=tag)


async def create_task(db: AsyncSession, *, category_id: str, prompt: str, template_id: str | None,
                      input_ids: list[uuid.UUID], apps: list[str] | None, options: dict[str, str] | None, speed: str,
                      ip: str | None) -> tuple[ArtcraftTask, Run]:
    cat = _category(category_id)
    tpl = catalog.template(cat, template_id)
    if template_id and tpl is None:
        raise _err(422, "bad_input", "알 수 없는 템플릿입니다.")
    chosen = list(dict.fromkeys(apps)) if apps else list(tpl.apps if tpl and tpl.apps else catalog.app_ids(cat))
    if not chosen or any(a not in catalog.app_ids(cat) for a in chosen):
        raise _err(422, "unknown_app", "이 범주에서 쓸 수 없는 앱이 포함돼 있어요.")
    clean = clean_options(tpl, options)
    await _load_inputs(db, cat, input_ids, tpl.min_files if tpl else 0)
    ws = await ensure_workspace(db)
    sess = ChatSession(workspace_id=ws.id, title=_title(prompt, tpl), engine="claude", kind=SessionKind.chat, pool="personal")
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
                    options: dict[str, str] | None, speed: str, ip: str | None) -> Run:
    if task.archived_at is not None:
        raise _err(409, "archived", "보관한 작업에는 이어서 지시할 수 없어요.")
    cat = _category(task.category)
    await _load_inputs(db, cat, input_ids)
    sess = await db.get(ChatSession, task.session_id)
    if sess is None:
        raise _err(404, "not_found", "작업 세션을 찾을 수 없습니다.")
    tpl = catalog.template(cat, task.template_id)
    clean = clean_options(tpl, options) if options else dict(task.options or {})
    task.updated_at = utcnow()
    return await _queue(db, sess, task, prompt, input_ids, clean, speed, use_previous, ip)


# ---------------------------------------------------------------- 조회

def _artifact(a: Artifact) -> dict[str, Any]:
    return ArtifactOut.from_model(a).model_dump(mode="json")


def _state(run: Run | None) -> str:
    return STATE_OF_RUN.get(run.status, "queued") if run is not None else "queued"


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
    if sids:
        rows = await db.execute(
            select(Artifact.session_id, func.count()).where(
                Artifact.session_id.in_(sids), Artifact.source == OUTPUT_SOURCE, Artifact.deleted_at.is_(None),
            ).group_by(Artifact.session_id))
        counts = {sid: n for sid, n in rows.all()}
    from ttakkari.api import desk_feedback
    last_runs = [rs[-1] for rs in runs.values() if rs]
    detailed = {r.id: r for r in await desk_feedback.runs_out(db, [r for r in last_runs if r.status in ACTIVE])}
    items = []
    for t in tasks:
        rs = runs.get(t.session_id, [])
        last = rs[-1] if rs else None
        live = detailed.get(last.id) if last is not None else None
        items.append({
            "id": str(t.id), "session_id": str(t.session_id), "category": t.category, "title": t.title, "apps": list(t.apps or []),
            "template_id": t.template_id, "state": _state(last), "round_count": len(rs), "output_count": counts.get(t.session_id, 0),
            "last_run_id": str(last.id) if last else None,
            "error": (last.error or "")[:300] if last is not None and last.status == RunStatus.failed else None,
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
        rounds.append({
            "id": str(run.id), "index": index, "status": run.status, "state": _state(run), "prompt": run.prompt,
            "result_text": (run.result_text or "")[:RESULT_CHARS] or None,
            "messages": messages.get(run.id, [])[-MESSAGES_KEEP:], "error": run.error,
            "options": meta.get("options") or {}, "use_previous": bool(meta.get("use_previous")),
            "inputs": [_artifact(inputs[uuid.UUID(str(i))]) for i in run.context_artifact_ids or [] if uuid.UUID(str(i)) in inputs],
            "outputs": [_artifact(a) for a in outputs.get(run.id, [])],
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
    """이 Run 보다 앞선 Run 중 결과가 있는 가장 최근 라운드의 결과 파일."""
    prior = list(await db.scalars(select(Run).where(Run.session_id == run.session_id, Run.created_at < run.created_at)
                                  .order_by(Run.created_at.desc())))
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
- 입력 파일(in/, prev/)은 고치지 말고 읽기만 한다. 고칠 때는 work/ 로 복사해 작업하고, 최종 결과는 {out} 에 의미 있는 이름(예: report-v2.docx)으로 저장한다. 그곳에 저장한 파일은 outbox로 복사돼 에이전트 파일로 자동 수집된다. 사용자가 대화에서 열어 보게 하려면 share_file 로 공유한다(AI 작업 파일에 남는다).
- 앱 MCP 도구가 안 보이거나 막히면 CLI 로 같은 일을 한다: {launcher} run <app> ... (명령 ID 목록은 `{launcher} run <app> commands`, 상세는 {agents}).
- 끝내기 전에 결과를 직접 확인한다(렌더·info·text 등으로 열어 보고, 이미지 결과는 PNG 로 렌더해 본다). 확인한 내용을 답에 한두 줄로 적는다.
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
    head.append(f"최종 결과 저장 위치: {folders['out']}")
    if log:
        head.append("주의: " + " / ".join(log))
    prompt = "\n".join(head) + "\n\n[사용자 지시]\n" + run.prompt

    launcher = str(artcraft.binary_path()) if artcraft.binary_ok() else "<artcraft 저장소>/bin/artcraft"
    agents_md = str(artcraft.root() / "AGENTS.md")
    recipes = "\n".join(f"  · {line.replace('artcraft ', launcher + ' ', 1) if 'artcraft run' in line else line}" for line in cat.cli)
    note = NOTE.format(category=cat.title, servers=", ".join(servers) or "없음(CLI 로 진행)", base=base, out=folders["out"],
                       launcher=launcher, agents=agents_md, recipes="앱별 CLI 요령:\n" + recipes if recipes else "")
    return RunPlan(prompt=prompt, note=note, mcp_servers=servers, out_dir=folders["out"], log=log)


def sweep_outputs(plan: RunPlan, run: Run, outbox: Path) -> int:
    """out/ 에 이 Run 이 새로 만든 파일을 outbox 로 복사한다. 이미 같은 이름·크기로 있으면 건너뛴다.

    기준은 Run 의 첫 시작 시각이다(한도 대기 뒤 재개해도 바뀌지 않고, 에이전트는 시작 뒤에야 파일을 만든다)."""
    since = (run.started_at or run.created_at).timestamp()
    copied = 0
    root = plan.out_dir
    if not root.is_dir():
        return 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            stat = path.stat()
            dest = outbox / path.relative_to(root)
            if stat.st_mtime < since or (dest.exists() and dest.stat().st_size == stat.st_size):
                continue
            _copy(path, dest)
            copied += 1
        except OSError:
            continue
    return copied
