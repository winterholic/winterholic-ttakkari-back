"""기존 운영 데이터 백필기(KB3-103): 기존 문서를 새 KB v3 파이프라인으로 재처리한다.

설계 요약
- 대상: 각 문서의 현재 리비전. 불변 원장(원본·리비전·패시지·근거)은 절대 UPDATE/DELETE 하지 않는다. 새 구조가 필요하면
  같은 문서에 새 리비전(supersedes_id)을 쌓고, 관측(KbObservation)으로 그 리비전을 현재로 만든다.
- 분류(문서마다 결정적): 이미 처리됨(skip_done) / 작업만 붙임(enqueue_only) / 구조 재청킹(rechunk_*) / 건너뜀(hidden·미분류) / 오류.
  새 청커의 계획(planned)과 현재 패시지의 경계·절 경로·종류가 같으면 재청킹이 필요 없다. 그래서 두 번째 실행은 아무것도 바꾸지 않는다(멱등).
- 레거시 수집기 HTML(trafilatura …+parse-1, 헤딩 평문)은 원본 바이트가 온전하면 현재 수집기 파서로 다시 추출해 헤딩 구조를 되살린다
  (rechunk_html_reparse). 원본이 없거나 손상됐으면 본문 그대로 구조 청킹만 한다(rechunk_same_body, 헤딩 정보는 복구되지 않음).
- 재개 가능: 상태는 DB 에서 다시 계산하므로 중단된 실행은 같은 명령을 다시 돌리면 이어진다. 진행은 보고서 디렉터리의
  progress.jsonl(문서별)·state.json(묶음별 커서·집계)에도 남긴다.
- 중단 안전: 문서마다 savepoint, 묶음마다 commit 이다. SIGINT/SIGTERM 은 현재 문서를 끝내고 커밋한 뒤 멈춘다. 강제 종료는 마지막 묶음만 잃는다.
- 롤백: rollback_backfill 이 되돌릴 리비전을 가리키는 새 관측을 덧붙여 이전 리비전을 현재로 되돌리고, 파생 주장 상태와 정본을 맞춘다
  (원장 행은 지우지 않는다).
- 기본은 dry-run 이다. 쓰기는 apply=True 일 때만 한다. 이 모듈은 운영 DB 를 가정하지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector import parse as collector_parse
from ttakkari.collector import textproc as collector_textproc
from ttakkari.collector.control_models import KbDocumentAccess, KbObservation
from ttakkari.common.chunking import chunk_structured_document
from ttakkari.config import get_settings
from ttakkari.kb_library import _latest_revision
from ttakkari.kb_models import KbDocument, KbPassage, KbRevision, KbSource
from ttakkari.kb_ontology.canonicals import rebuild_canonical
from ttakkari.kb_ontology_models import KbAssertion
from ttakkari.kb_pipeline import enqueue_pipeline_job
from ttakkari.kb_pipeline_models import KbPipelineJob
from ttakkari.kb_store import originals_root, segment_revision, sha256_hex
from ttakkari.models import utcnow

log = logging.getLogger(__name__)

BACKFILL_PARSER_VERSION = "kb3-backfill-structure-v1"
BACKFILL_LOCK_KEY = "kb3-backfill"
NAMESPACE = uuid.UUID("5f0c6f0e-6a1b-4c43-9a2c-0b6f3f6e1b01")

SKIP_DONE = "skip_done"
SKIP_HIDDEN = "skip_hidden"
SKIP_UNCLASSIFIED = "skip_unclassified"
ENQUEUE_ONLY = "enqueue_only"
RECHUNK_HTML = "rechunk_html_reparse"
RECHUNK_SAME_BODY = "rechunk_same_body"
ERROR = "error"
WRITE_ACTIONS = frozenset({ENQUEUE_ONLY, RECHUNK_HTML, RECHUNK_SAME_BODY})


class BackfillAlreadyRunning(RuntimeError):
    pass


@dataclass
class Plan:
    document_id: uuid.UUID
    title: str
    action: str
    reason: str
    revision_id: uuid.UUID | None = None
    rev_no: int | None = None
    passages_before: int = 0
    passages_after: int | None = None
    chars_before: int = 0
    chars_after: int | None = None
    new_body: str | None = field(default=None, repr=False)
    new_parser_version: str | None = None
    has_job: bool = False
    error: str | None = None

    def record(self) -> dict[str, Any]:
        out = {k: v for k, v in asdict(self).items() if k != "new_body"}
        out["document_id"] = str(self.document_id)
        out["revision_id"] = str(self.revision_id) if self.revision_id else None
        return out


@dataclass
class RunSummary:
    run_id: str
    apply: bool
    status: str = "running"
    examined: int = 0
    by_action: Counter = field(default_factory=Counter)
    batches: int = 0
    passages_before: int = 0
    passages_after: int = 0
    single_passage_before: int = 0
    single_passage_after: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cursor: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "apply": self.apply,
            "status": self.status,
            "examined": self.examined,
            "by_action": dict(self.by_action),
            "batches": self.batches,
            "passages_before": self.passages_before,
            "passages_after": self.passages_after,
            "single_passage_documents_before": self.single_passage_before,
            "single_passage_documents_after": self.single_passage_after,
            "errors": self.errors[:50],
            "error_count": len(self.errors),
            "elapsed_seconds": round((self.finished_at or time.time()) - self.started_at, 3),
            "cursor": self.cursor,
        }


def _structure_key(chunks) -> list[tuple]:
    return [(c.char_start, c.char_end, tuple(c.section_path), c.chunk_kind) for c in chunks]


def _existing_key(passages: list[KbPassage]) -> list[tuple]:
    return [(p.char_start, p.char_end, tuple(p.section_path or ()), p.chunk_kind) for p in passages]


def _is_single_unstructured(passage_count: int, char_count: int) -> bool:
    return passage_count == 1 and char_count > 1200


async def _passages(db: AsyncSession, revision_id: uuid.UUID) -> list[KbPassage]:
    return list(await db.scalars(select(KbPassage).where(KbPassage.revision_id == revision_id).order_by(KbPassage.ord)))


def _raw_html_body(source: KbSource) -> tuple[str | None, str]:
    """레거시 수집기 HTML 의 원본 바이트로 헤딩 구조 본문을 다시 만든다. (본문|None, 사유)."""
    path = originals_root() / source.store_path
    try:
        raw = path.read_bytes()
    except OSError:
        return None, "original_missing"
    if hashlib.sha256(raw).hexdigest() != source.sha256:
        return None, "original_corrupt"
    try:
        extracted = collector_parse.extract_document(raw, url=source.locator or "https://backfill.invalid/", kind="html")
    except ValueError:
        return None, "html_empty_extraction"
    body = collector_textproc.normalize_text(extracted.text).text
    if not body.strip() or "\x00" in body:
        return None, "html_empty_extraction"
    return body, "ok"


async def classify(db: AsyncSession, doc: KbDocument, *, classify_legacy: bool, include_hidden: bool) -> Plan:
    plan = Plan(document_id=doc.id, title=doc.title, action=SKIP_DONE, reason="")
    access = await db.get(KbDocumentAccess, doc.id)
    if access is not None and access.hidden and not include_hidden:
        return Plan(doc.id, doc.title, SKIP_HIDDEN, "hidden_by_collection_policy")
    if access is None and not classify_legacy:
        # 접근 행이 없는 문서는 work_only 취급된다. 백필 계획에서 처리하려면 --classify-legacy 로 안전하게 분류하거나 사전에 kb-backfill-access 를 실행해야 한다.
        return Plan(doc.id, doc.title, SKIP_UNCLASSIFIED, "no_access_row_needs_classify_legacy_decision")
    rev = await _latest_revision(db, doc.id)
    if rev is None:
        return Plan(doc.id, doc.title, SKIP_DONE, "no_revision")
    plan.revision_id, plan.rev_no = rev.id, rev.rev_no
    plan.chars_before = plan.chars_after = rev.char_count
    existing = await _passages(db, rev.id)
    plan.passages_before = len(existing)
    plan.has_job = await db.scalar(select(KbPipelineJob.id).where(KbPipelineJob.revision_id == rev.id)) is not None

    planned = chunk_structured_document(rev.body, None, min_chars=0)
    source = await db.get(KbSource, rev.source_id)

    # 레거시 수집기 HTML(trafilatura …+parse-1)은 헤딩 표시를 잃은 평문이라 파이프라인 parsing 단계가 held 로 막는다
    # (collector_html_structure_revision_required). 패시지 경계가 우연히 계획과 같아도 새 리비전이 필요하다.
    if source is not None and _is_legacy_collector_html(source, rev):
        new_body, why = _raw_html_body(source)
        plan.new_body = new_body if new_body is not None else rev.body
        if new_body is not None:
            plan.new_parser_version = collector_parse.PARSER_VERSION[:60]
            reason = "legacy_html_structure_recovered_from_original"
            action = RECHUNK_HTML
        else:
            # 원본이 없거나 손상됐다: 헤딩은 복구할 수 없다. 본문 그대로 구조 청킹한 리비전으로 지식화하되 사유를 남긴다.
            plan.new_parser_version = BACKFILL_PARSER_VERSION
            reason = f"structure_from_text_only({why})"
            action = RECHUNK_SAME_BODY
        plan.chars_after = len(plan.new_body)
        plan.passages_after = len(chunk_structured_document(plan.new_body, None, min_chars=0))
        return replace_plan(plan, action, reason)

    structured = bool(existing) and _existing_key(existing) == _structure_key(planned)
    if not existing:
        # 패시지가 하나도 없는 리비전은 새 리비전 없이 그 자리에 구조 패시지를 만든다(패시지는 추가만 가능한 원장이다).
        plan.passages_after = len(planned)
        return replace_plan(plan, ENQUEUE_ONLY, "no_passages_segment_in_place")
    if structured:
        plan.passages_after = len(existing)
        if plan.has_job:
            return replace_plan(plan, SKIP_DONE, "structured_and_has_job")
        return replace_plan(plan, ENQUEUE_ONLY, "structured_without_job")

    plan.new_body = rev.body
    plan.new_parser_version = BACKFILL_PARSER_VERSION
    plan.passages_after = len(planned)
    return replace_plan(plan, RECHUNK_SAME_BODY, "unstructured_passages")


def _is_legacy_collector_html(source: KbSource, revision: KbRevision) -> bool:
    media = (source.media_type or "").split(";")[0].strip().lower()
    parser_version = revision.parser_version or ""
    return media in ("text/html", "application/xhtml+xml") and parser_version.startswith("trafilatura-") and not parser_version.endswith("+parse-2")


def replace_plan(plan: Plan, action: str, reason: str) -> Plan:
    plan.action, plan.reason = action, reason
    return plan


async def _create_revision(db: AsyncSession, doc: KbDocument, prev: KbRevision, plan: Plan) -> KbRevision:
    """같은 문서·같은 원본 위에 새 리비전을 쌓는다. 같은 (파서 버전, 본문 해시)가 이미 있으면 그것을 재사용한다(재개 안전)."""
    body = plan.new_body or prev.body
    digest = sha256_hex(body.encode("utf-8"))
    # 수집(ingest_source)과 같은 키로 문서 단위 직렬화한다: rev_no 경쟁을 막는다.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "kb-ingest:" + doc.doc_key})
    existing = await db.scalar(
        select(KbRevision).where(
            KbRevision.document_id == doc.id, KbRevision.parser_version == plan.new_parser_version, KbRevision.text_sha256 == digest
        )
    )
    if existing is not None:
        return existing
    last_no = await db.scalar(select(func.max(KbRevision.rev_no)).where(KbRevision.document_id == doc.id)) or 0
    revision_id = uuid.uuid4()
    await db.execute(
        insert(KbRevision).values(
            id=revision_id, document_id=doc.id, source_id=prev.source_id, rev_no=last_no + 1,
            parser_version=plan.new_parser_version, body=body, text_sha256=digest, char_count=len(body),
            supersedes_id=prev.id, created_at=utcnow(),
        )
    )
    await db.flush()
    return await db.get(KbRevision, revision_id)


async def _make_current(db: AsyncSession, doc: KbDocument, new_rev: KbRevision, prev: KbRevision, run_id: str) -> None:
    """새 리비전을 가리키는 관측을 덧붙여 현재로 만든다(관측이 있으면 관측이 현재성을 정한다).

    관측이 없던 문서(업로드 등)에도 관측을 만든다: 그래야 롤백이 이전 리비전을 가리키는 관측을 덧붙여 되돌릴 수 있다.
    이미 최신 관측이 새 리비전이면 아무것도 하지 않는다(멱등). 롤백으로 최신 관측이 이전 리비전을 가리키게 된 뒤 다시 백필하면
    그 최신 관측에 묶인 새 관측을 한 번 더한다(같은 상태에서 다시 부르면 같은 envelope 라서 중복되지 않는다).
    """
    latest = await db.scalar(
        select(KbObservation).where(KbObservation.document_id == doc.id)
        .order_by(KbObservation.event_time.desc(), KbObservation.recorded_at.desc(), KbObservation.id.desc()).limit(1)
    )
    if latest is not None and latest.revision_id == new_rev.id:
        return
    envelope = uuid.uuid5(NAMESPACE, f"backfill:{new_rev.id}:after:{latest.id if latest else 'none'}")
    if await db.scalar(select(KbObservation.id).where(KbObservation.envelope_id == envelope)) is not None:
        return
    floor = latest.event_time if latest is not None else prev.created_at
    db.add(KbObservation(
        document_id=doc.id, revision_id=new_rev.id, envelope_id=envelope,
        event_time=max(utcnow(), floor + timedelta(milliseconds=1)),
        provenance={"backfill": run_id, "supersedes_revision_id": str(prev.id), "action": new_rev.parser_version},
    ))


async def apply_plan(db: AsyncSession, plan: Plan, run_id: str, *, classify_legacy: bool) -> Plan:
    doc = await db.get(KbDocument, plan.document_id)
    prev = await db.get(KbRevision, plan.revision_id)
    if classify_legacy and await db.get(KbDocumentAccess, doc.id) is None:
        # 접근 행이 없는 문서는 안전 규칙에 따라 분류한다 (알 수 없으면 work_only, 근거 있는 분류만 personal).
        from ttakkari.kb_access_backfill import classify as classify_access
        source = await db.get(KbSource, prev.source_id) if prev else None
        scope, _rule = classify_access(doc.doc_key, source.origin_meta if source else None)
        origin_scope = "personal" if scope == "personal" else "work"
        visibility = "global" if scope == "personal" else "work_only"
        db.add(KbDocumentAccess(document_id=doc.id, tenant_id="owner", scope=scope, origin_scope=origin_scope, visibility=visibility))
        await db.flush()
    if plan.action == ENQUEUE_ONLY:
        await segment_revision(db, prev.id)  # 패시지가 이미 있으면 같은 행이 남는다(멱등)
        await enqueue_pipeline_job(db, document_id=doc.id, revision_id=prev.id)
        plan.passages_after = await db.scalar(select(func.count()).select_from(KbPassage).where(KbPassage.revision_id == prev.id))
        return plan
    new_rev = await _create_revision(db, doc, prev, plan)
    await segment_revision(db, new_rev.id)
    await enqueue_pipeline_job(db, document_id=doc.id, revision_id=new_rev.id)
    await _make_current(db, doc, new_rev, prev, run_id)
    plan.revision_id, plan.rev_no = new_rev.id, new_rev.rev_no
    plan.chars_after = new_rev.char_count
    plan.passages_after = await db.scalar(select(func.count()).select_from(KbPassage).where(KbPassage.revision_id == new_rev.id))
    return plan


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    tmp.replace(path)


async def run_backfill(
    session_factory: Callable[[], AsyncSession], *, apply: bool = False, batch_size: int = 10, limit: int | None = None,
    run_id: str | None = None, report_dir: Path | None = None, classify_legacy: bool = False, include_hidden: bool = False,
    stop: asyncio.Event | None = None, pause_seconds: float = 0.0,
) -> RunSummary:
    """문서를 created_at 순 keyset 으로 batch_size 개씩 처리한다. apply=False 면 아무것도 쓰지 않는다(분류 보고만)."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    summary = RunSummary(run_id=run_id or time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6], apply=apply)
    progress = None
    if report_dir is not None:
        report_dir = Path(report_dir) / summary.run_id
        report_dir.mkdir(parents=True, exist_ok=True)
        progress = (report_dir / "progress.jsonl").open("a", encoding="utf-8")
    lock_session = session_factory()
    try:
        if apply:
            # 세션 수준 advisory lock: 같은 백필이 둘 이상 돌지 않게 한다. 프로세스가 죽으면 연결과 함께 풀린다.
            got = await lock_session.scalar(text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"), {"k": BACKFILL_LOCK_KEY})
            if not got:
                raise BackfillAlreadyRunning("another kb backfill holds the advisory lock")
        cursor: tuple | None = None
        while True:
            if stop is not None and stop.is_set():
                summary.status = "stopped"
                break
            async with session_factory() as db:
                stmt = select(KbDocument).order_by(KbDocument.created_at, KbDocument.id).limit(batch_size)
                if cursor is not None:
                    stmt = stmt.where((KbDocument.created_at, KbDocument.id) > cursor)
                docs = list(await db.scalars(stmt))
                if not docs:
                    summary.status = "completed" if summary.status == "running" else summary.status
                    break
                for doc in docs:
                    cursor = (doc.created_at, doc.id)
                    summary.cursor = {"created_at": doc.created_at.isoformat(), "document_id": str(doc.id)}
                    if limit is not None and summary.examined >= limit:
                        summary.status = "limit_reached"
                        break
                    if stop is not None and stop.is_set():
                        summary.status = "stopped"
                        break
                    plan = await _process_one(db, doc, summary, apply=apply, run_id=summary.run_id, classify_legacy=classify_legacy,
                                              include_hidden=include_hidden)
                    summary.examined += 1
                    summary.by_action[plan.action] += 1
                    if plan.action not in (SKIP_HIDDEN, SKIP_UNCLASSIFIED, ERROR):
                        after = plan.passages_after if plan.passages_after is not None else plan.passages_before
                        summary.passages_before += plan.passages_before
                        summary.passages_after += after
                        summary.single_passage_before += _is_single_unstructured(plan.passages_before, plan.chars_before)
                        summary.single_passage_after += _is_single_unstructured(
                            after, plan.chars_after if plan.chars_after is not None else plan.chars_before
                        )
                    if progress is not None:
                        progress.write(json.dumps({"ts": time.time(), "run_id": summary.run_id, "apply": apply, **plan.record()},
                                                  ensure_ascii=False, default=str) + "\n")
                        progress.flush()
                if apply:
                    await db.commit()
                else:
                    await db.rollback()
            summary.batches += 1
            if report_dir is not None:
                _write_json(report_dir / "state.json", summary.as_dict())
            if summary.status in ("stopped", "limit_reached"):
                break
            if pause_seconds:
                await asyncio.sleep(pause_seconds)
    finally:
        if apply:
            try:
                await lock_session.execute(text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": BACKFILL_LOCK_KEY})
            finally:
                await lock_session.close()
        else:
            await lock_session.close()
        summary.finished_at = time.time()
        if progress is not None:
            progress.close()
        if report_dir is not None:
            _write_json(report_dir / "state.json", summary.as_dict())
    return summary


async def _process_one(
    db: AsyncSession, doc: KbDocument, summary: RunSummary, *, apply: bool, run_id: str, classify_legacy: bool, include_hidden: bool,
) -> Plan:
    try:
        # 분류는 읽기만 하지만 실패해도 이 문서만 건너뛴다. 쓰기는 savepoint 안에서 해 한 문서의 실패가 묶음을 오염하지 않는다.
        plan = await classify(db, doc, classify_legacy=classify_legacy, include_hidden=include_hidden)
        if apply and plan.action in WRITE_ACTIONS:
            async with db.begin_nested():
                plan = await apply_plan(db, plan, run_id, classify_legacy=classify_legacy)
        return plan
    except Exception as exc:  # 한 문서의 실패가 백필 전체를 멈추지 않는다. 보고서에 남기고 다음 실행에서 다시 시도한다.
        log.exception("kb backfill failed for %s", doc.id)
        summary.errors.append({"document_id": str(doc.id), "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        return Plan(doc.id, doc.title, ERROR, "exception", error=type(exc).__name__)


async def rollback_backfill(
    session_factory: Callable[[], AsyncSession], *, run_id: str | None = None, apply: bool = False,
) -> dict[str, Any]:
    """백필이 쌓은 리비전을 되돌린다: 이전 리비전을 가리키는 새 관측을 덧붙여 이전 리비전을 현재로 만든다.

    - 원장(리비전·패시지·근거)은 그대로 남는다. 현재성만 바뀐다.
    - 되돌린 리비전의 active 주장은 superseded 로, 이전 리비전의 superseded 주장은 active 로 맞추고 관련 정본을 다시 만든다.
    - 백필 이후 같은 문서에 새 관측(재수집)이 있으면 그 문서는 되돌리지 않는다(내용이 이미 바뀌었다).
    """
    result: dict[str, Any] = {"apply": apply, "run_id": run_id, "reverted": [], "skipped": []}
    async with session_factory() as db:
        stmt = select(KbObservation).where(text("kb_observations.provenance ? 'backfill'")).order_by(KbObservation.recorded_at)
        if run_id:
            stmt = stmt.where(KbObservation.provenance["backfill"].astext == run_id)
        for obs in await db.scalars(stmt):
            latest = await db.scalar(
                select(KbObservation).where(KbObservation.document_id == obs.document_id)
                .order_by(KbObservation.event_time.desc(), KbObservation.recorded_at.desc(), KbObservation.id.desc()).limit(1)
            )
            if latest is None or latest.id != obs.id:
                result["skipped"].append({"document_id": str(obs.document_id), "reason": "newer_observation_exists"})
                continue
            prev_id = obs.provenance.get("supersedes_revision_id")
            if not prev_id:
                result["skipped"].append({"document_id": str(obs.document_id), "reason": "no_previous_revision"})
                continue
            item = {"document_id": str(obs.document_id), "from_revision_id": str(obs.revision_id), "to_revision_id": prev_id}
            if apply:
                envelope = uuid.uuid5(NAMESPACE, f"backfill-rollback:{obs.revision_id}")
                if await db.scalar(select(KbObservation.id).where(KbObservation.envelope_id == envelope)) is None:
                    db.add(KbObservation(
                        document_id=obs.document_id, revision_id=uuid.UUID(prev_id), envelope_id=envelope,
                        event_time=max(utcnow(), obs.event_time + timedelta(milliseconds=1)),
                        provenance={"backfill_rollback": obs.provenance["backfill"], "reverts_revision_id": str(obs.revision_id)},
                    ))
                    await db.flush()
                entity_ids: set[uuid.UUID] = set()
                for a in await db.scalars(select(KbAssertion).where(KbAssertion.revision_id == obs.revision_id)):
                    entity_ids.add(a.subject_entity_id)
                    if a.status == "active":
                        a.status, a.is_canonical = "superseded", False
                for a in await db.scalars(select(KbAssertion).where(KbAssertion.revision_id == uuid.UUID(prev_id))):
                    entity_ids.add(a.subject_entity_id)
                    if a.status == "superseded":
                        a.status = "active"
                await db.flush()
                for entity_id in entity_ids:
                    await rebuild_canonical(db, entity_id)
            result["reverted"].append(item)
        if apply:
            await db.commit()
        else:
            await db.rollback()
    return result


def default_report_dir() -> Path:
    return get_settings().kb_originals_dir.parent / "backfill"


BACKFILL_JOBS_LOCK_KEY = "kb3-backfill-jobs"


@dataclass
class JobBackfillSummary:
    run_id: str
    apply: bool
    examined: int = 0
    enqueued: int = 0
    skipped_has_job: int = 0
    skipped_hidden: int = 0
    skipped_no_revision: int = 0
    batches: int = 0
    status: str = "completed"
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def run_enqueue_jobs_backfill(
    session_factory: Callable[[], AsyncSession],
    *,
    apply: bool = False,
    batch_size: int = 10,
    limit: int | None = None,
    run_id: str | None = None,
    report_dir: Path | None = None,
    include_hidden: bool = False,
    stop: asyncio.Event | None = None,
    pause_seconds: float = 0.0,
) -> JobBackfillSummary:
    """구 세대 코드 혹은 배포 겹침 구간에 등록되어 파이프라인 잡이 없는 현재 리비전에 파이프라인 잡을 만드는 멱등 백필."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    start = time.monotonic()
    summary = JobBackfillSummary(
        run_id=run_id or time.strftime("%Y%m%dT%H%M%S") + "-jobs-" + uuid.uuid4().hex[:6],
        apply=apply,
    )
    rep_dir: Path | None = None
    progress = None
    if report_dir is not None:
        rep_dir = Path(report_dir) / summary.run_id
        rep_dir.mkdir(parents=True, exist_ok=True)
        progress = (rep_dir / "progress.jsonl").open("a", encoding="utf-8")

    lock_session = session_factory()
    try:
        if apply:
            got = await lock_session.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"),
                {"k": BACKFILL_JOBS_LOCK_KEY},
            )
            if not got:
                raise BackfillAlreadyRunning("another kb job backfill holds the advisory lock")
        cursor: tuple | None = None
        while True:
            if stop is not None and stop.is_set():
                summary.status = "stopped"
                break
            async with session_factory() as db:
                stmt = select(KbDocument).order_by(KbDocument.created_at, KbDocument.id).limit(batch_size)
                if cursor is not None:
                    stmt = stmt.where((KbDocument.created_at, KbDocument.id) > cursor)
                docs = list(await db.scalars(stmt))
                if not docs:
                    break
                for doc in docs:
                    cursor = (doc.created_at, doc.id)
                    if limit is not None and summary.examined >= limit:
                        summary.status = "limit_reached"
                        break
                    if stop is not None and stop.is_set():
                        summary.status = "stopped"
                        break
                    summary.examined += 1

                    access = await db.get(KbDocumentAccess, doc.id)
                    if access is not None and access.hidden and not include_hidden:
                        summary.skipped_hidden += 1
                        if progress is not None:
                            progress.write(json.dumps({
                                "ts": time.time(), "run_id": summary.run_id, "document_id": str(doc.id),
                                "action": "skip_hidden", "title": doc.title,
                            }, ensure_ascii=False) + "\n")
                            progress.flush()
                        continue

                    rev = await _latest_revision(db, doc.id)
                    if rev is None:
                        summary.skipped_no_revision += 1
                        if progress is not None:
                            progress.write(json.dumps({
                                "ts": time.time(), "run_id": summary.run_id, "document_id": str(doc.id),
                                "action": "skip_no_revision", "title": doc.title,
                            }, ensure_ascii=False) + "\n")
                            progress.flush()
                        continue

                    existing_job = await db.scalar(
                        select(KbPipelineJob.id).where(KbPipelineJob.revision_id == rev.id).limit(1)
                    )
                    if existing_job is not None:
                        summary.skipped_has_job += 1
                        if progress is not None:
                            progress.write(json.dumps({
                                "ts": time.time(), "run_id": summary.run_id, "document_id": str(doc.id),
                                "revision_id": str(rev.id), "action": "skip_has_job", "title": doc.title,
                            }, ensure_ascii=False) + "\n")
                            progress.flush()
                        continue

                    if apply:
                        await enqueue_pipeline_job(db, document_id=doc.id, revision_id=rev.id)
                    summary.enqueued += 1
                    if progress is not None:
                        progress.write(json.dumps({
                            "ts": time.time(), "run_id": summary.run_id, "document_id": str(doc.id),
                            "revision_id": str(rev.id), "action": "enqueued" if apply else "plan_enqueue",
                            "title": doc.title,
                        }, ensure_ascii=False) + "\n")
                        progress.flush()

                if apply:
                    await db.commit()
                else:
                    await db.rollback()
            summary.batches += 1
            if rep_dir is not None:
                _write_json(rep_dir / "state.json", summary.as_dict())
            if summary.status in ("stopped", "limit_reached"):
                break
            if pause_seconds:
                await asyncio.sleep(pause_seconds)
    finally:
        if progress is not None:
            progress.close()
        if apply:
            try:
                await lock_session.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"),
                    {"k": BACKFILL_JOBS_LOCK_KEY},
                )
            except Exception:
                pass
        await lock_session.close()

    summary.duration_seconds = round(time.monotonic() - start, 3)
    if rep_dir is not None:
        _write_json(rep_dir / "state.json", summary.as_dict())
    return summary
