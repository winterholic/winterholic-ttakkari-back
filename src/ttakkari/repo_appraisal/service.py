"""저장소 가치 평가 실행: 캐시·백그라운드 작업·이력 delta·재시작 정리(ADR 0073)."""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select

from ttakkari import db as dbmod
from ttakkari.config import get_settings
from ttakkari.github.errors import GithubError
from ttakkari.github.service import GithubService
from ttakkari.models import RepoAppraisal, utcnow
from ttakkari.repo_appraisal import collect as collect_mod
from ttakkari.repo_appraisal import evidence as evidence_mod
from ttakkari.repo_appraisal import judge as judge_mod
from ttakkari.repo_appraisal import rubric

log = logging.getLogger(__name__)

MODES = ("quick", "deep")
# 같은 (owner/repo, head_sha, mode, rubric_version) 완료 행을 재사용한다(토큰 0).
LLM_RUNNER: Callable[[str, str], Awaitable[tuple[str | None, str | None]]] | None = None
_PENDING: dict[uuid.UUID, str] = {}  # 행 id → GitHub 계정. 메모리 전용이라 재시작이면 함께 사라진다.
_TASKS: set[asyncio.Task] = set()


async def start(db, svc: GithubService, *, target: str, account: str, mode: str,
                force: bool) -> tuple[RepoAppraisal, bool]:
    """캐시 hit 이면 (완료 행, True), 아니면 (새 행, False)과 백그라운드 실행."""
    if mode not in MODES:
        raise GithubError("invalid_target", "mode 는 quick·deep 중 하나여야 합니다.")
    owner, repo, sha, _branch = await collect_mod.resolve_head(svc, account, target)
    if not force:
        hit = await db.scalar(select(RepoAppraisal).where(
            RepoAppraisal.owner == owner, RepoAppraisal.repo == repo,
            RepoAppraisal.head_sha == sha, RepoAppraisal.mode == mode,
            RepoAppraisal.rubric_version == rubric.RUBRIC_VERSION,
            RepoAppraisal.status == "done").order_by(RepoAppraisal.created_at.desc()))
        if hit is not None:
            return hit, True
    row = RepoAppraisal(owner=owner, repo=repo, head_sha=sha, mode=mode,
                        rubric_version=rubric.RUBRIC_VERSION, status="queued",
                        result={"stage": "collect"})
    db.add(row)
    await db.flush()
    _PENDING[row.id] = account
    task = asyncio.create_task(run_appraisal(row.id, svc))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return row, False


async def run_appraisal(row_id: uuid.UUID, svc: GithubService) -> None:
    """백그라운드 실행. 자기 세션을 쓰고 단계마다 저장한다."""
    try:
        async with dbmod.sessionmaker()() as db:
            row = await db.get(RepoAppraisal, row_id)
            if row is None or row.status not in ("queued", "running"):
                return
            account = _PENDING.pop(row_id, None)
            if account is None:
                _fail(row, "계정 정보가 없습니다(서버 재시작). 다시 요청하세요.")
                await db.commit()
                return
            row.status = "running"
            await _stage(db, row, "collect")
            s = get_settings()
            try:
                got = await collect_mod.collect(svc, account, row.owner, row.repo, row.mode)
            except GithubError as exc:
                _fail(row, f"{exc.code}: {exc.message}")
                await db.commit()
                return
            await _stage(db, row, "score")
            repo_type, reasons = rubric.classify_repo_type(got.facts)
            det = (rubric.score_code_quality(got.facts, repo_type)
                   + rubric.score_architecture(got.facts)
                   + rubric.score_maintainability(got.facts)
                   + rubric.score_ai_harness(got.facts))
            by_axis: dict[str, list] = {a: [] for a in rubric.AXES}
            for item in det:
                by_axis[item.axis].append(item)
            by_axis["originality"] = [rubric.ItemScore("originality", "verdict", None, "pending", {})]
            pending = [i.key for items in by_axis.values() for i in items if i.status == "pending"
                       and i.key in rubric.LLM_ITEMS]
            await _stage(db, row, "llm")
            packet = evidence_mod.build_packet(
                owner=row.owner, repo=row.repo, head_sha=got.head_sha, repo_type=repo_type,
                facts=got.facts, tree_truncated=got.tree_truncated, mode=row.mode,
                max_chars=s.repo_appraisal_evidence_max_chars)
            llm_used, summary = False, ""
            if pending:
                text, error = await _call_judge(packet.text, repo_type, pending, s)
                parsed = judge_mod.parse_output(text)
                if parsed is not None:
                    llm_used = True
                    flat = [i for items in by_axis.values() for i in items]
                    rubric.apply_llm_scores(flat, parsed, valid_paths=packet.valid_paths)
                    summary = parsed.get("summary") if isinstance(parsed.get("summary"), str) else ""
                elif error:
                    log.warning("repo appraisal llm skipped: %s", error[:200])
            for items in by_axis.values():
                for i in items:
                    if i.status == "pending":  # LLM 을 안 썼으면 주관 항목은 미채점이다
                        i.status, i.evidence = "unavailable", {"reason": "LLM 미사용"}
            axes = rubric.axis_scores(by_axis)
            score, contrib = rubric.weighted_score(axes, repo_type)
            items = [i.to_dict() for items in by_axis.values() for i in items]
            row.head_sha, row.repo_type = got.head_sha, repo_type
            row.result = {"repo_type": repo_type, "axes": axes, "items": items,
                          "weights": rubric.WEIGHTS[repo_type],
                          "contributions": contrib,
                          "weaknesses": rubric.top_weaknesses(
                              [i for items in by_axis.values() for i in items], axes, repo_type),
                          "summary": summary, "repo_type_reasons": reasons,
                          "tree_truncated": got.tree_truncated, "unavailable": got.unavailable,
                          "requests": got.requests}
            row.weighted_score, row.grade = score, rubric.grade_for(score)
            row.llm_used, row.evidence_chars = llm_used, packet.evidence_chars
            row.llm_model = f"{s.repo_appraisal_engine}:{s.repo_appraisal_model}" if llm_used else None
            row.status, row.finished_at, row.error = "done", utcnow(), None
            await db.commit()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - 백그라운드 실패는 행에 남기고 끝낸다
        log.warning("repo appraisal failed: %s", e, exc_info=False)
        try:
            async with dbmod.sessionmaker()() as db:
                row = await db.get(RepoAppraisal, row_id)
                if row is not None and row.status in ("queued", "running"):
                    _fail(row, f"internal: {type(e).__name__}")
                    await db.commit()
        except Exception:  # noqa: BLE001, S110
            pass


async def _call_judge(packet_text: str, repo_type: str, pending: list[str], s) -> tuple[Any, Any]:
    prompt = judge_mod.build_prompt(packet_text, repo_type, pending)
    try:
        return await judge_mod.call_model(judge_mod.SYSTEM_PROMPT, prompt,
                                          engine=s.repo_appraisal_engine,
                                          model=s.repo_appraisal_model,
                                          timeout=s.repo_appraisal_timeout_seconds,
                                          runner=LLM_RUNNER)
    except Exception as e:  # noqa: BLE001 - 호출 실패는 결정론 점수만으로 끝낸다
        return None, str(e)


async def _stage(db, row: RepoAppraisal, stage: str) -> None:
    row.result = {"stage": stage}
    await db.commit()


def _fail(row: RepoAppraisal, error: str) -> None:
    row.status, row.error, row.finished_at = "failed", error, utcnow()


async def previous_score(db, row: RepoAppraisal) -> float | None:
    prev = await db.scalar(select(RepoAppraisal.weighted_score).where(
        RepoAppraisal.owner == row.owner, RepoAppraisal.repo == row.repo,
        RepoAppraisal.status == "done", RepoAppraisal.created_at < row.created_at)
        .order_by(RepoAppraisal.created_at.desc()))
    return prev


def detail_view(row: RepoAppraisal, delta: float | None) -> dict[str, Any]:
    stage = row.result.get("stage") if isinstance(row.result, dict) else None
    return {"id": str(row.id), "owner": row.owner, "repo": row.repo, "head_sha": row.head_sha,
            "mode": row.mode, "rubric_version": row.rubric_version, "repo_type": row.repo_type,
            "status": row.status,
            **({"stage": stage} if row.status in ("queued", "running") and stage else {}),
            "result": row.result if row.status == "done" else None,
            "weighted_score": row.weighted_score, "grade": row.grade,
            "llm_used": row.llm_used, "llm_model": row.llm_model,
            "evidence_chars": row.evidence_chars, "error": row.error, "delta": delta,
            "created_at": _iso(row.created_at), "finished_at": _iso(row.finished_at)}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def recover_running() -> int:
    """재시작 정리: queued·running 을 실패로 둔다. queued 는 워커가 없어 재개 불가라 함께 둔다."""
    async with dbmod.sessionmaker()() as db:
        rows = list(await db.scalars(select(RepoAppraisal).where(
            RepoAppraisal.status.in_(["queued", "running"]))))
        for row in rows:
            _fail(row, "서버 재시작으로 중단")
        await db.commit()
        return len(rows)
