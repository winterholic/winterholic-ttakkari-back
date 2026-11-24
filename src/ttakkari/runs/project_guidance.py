"""Owner-registered, project-scoped instructions for work Runs."""

import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import (
    ChatSession,
    CtxMemory,
    Run,
    RunOrigin,
    SessionKind,
    Workspace,
)

RULE_PREFIX = "run-project-guidance:"
RECENT_CONTEXT_MARKER = "[최근 대화 — 참고만]"
TASK_ORIGINS = (RunOrigin.user, RunOrigin.desk, RunOrigin.orchestrator)


def _task_text(prompt: str) -> str:
    return prompt.partition(RECENT_CONTEXT_MARKER)[0]


async def prompt_note(db: AsyncSession, session: ChatSession, run: Run) -> str:
    if session.pool != "work" or session.kind == SessionKind.desk:
        return ""

    rules = list(await db.scalars(
        select(CtxMemory).where(
            CtxMemory.scope == "work",
            CtxMemory.kind == "instruction",
            CtxMemory.source == "manual",
            CtxMemory.pinned.is_(False),
            CtxMemory.archived_at.is_(None),
            CtxMemory.source_ref.startswith(RULE_PREFIX, autoescape=True),
        ).order_by(CtxMemory.created_at, CtxMemory.id)
    ))
    if not rules:
        return ""

    workspace = await db.get(Workspace, run.workspace_id)
    candidates = [workspace.name, Path(workspace.root_path).name] if workspace else []
    if run.origin in TASK_ORIGINS:
        candidates.append(_task_text(run.prompt))

    # A generic work workspace can contain several projects. The original
    # task identifies short continuations and descendants with generic titles.
    current = session
    for _ in range(3):
        candidates.append(current.title)
        first_prompt = await db.scalar(
            select(Run.prompt).where(Run.session_id == current.id, Run.origin.in_(TASK_ORIGINS))
            .order_by(Run.created_at, Run.id).limit(1)
        )
        if first_prompt:
            candidates.append(_task_text(first_prompt))
        if current.kind != SessionKind.worker or current.parent_id is None:
            break
        parent = await db.get(ChatSession, current.parent_id)
        if parent is None or parent.pool != "work":
            break
        current = parent

    text = "\n".join(candidates)
    notes: dict[str, str] = {}
    for rule in rules:
        aliases = [alias.strip() for alias in rule.source_ref.removeprefix(RULE_PREFIX).split(",") if alias.strip()]
        if aliases and re.search(r"(?<!\w)(?:" + "|".join(map(re.escape, aliases)) + r")(?!\w)", text, re.IGNORECASE):
            notes[rule.source_ref] = rule.content
    if not notes:
        return ""
    return "[프로젝트 작업 규칙]\n" + "\n\n".join(notes.values())
