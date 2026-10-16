"""Transactional source ledger and evidence-bound assertions.

Functions flush but do not commit: an API/import caller owns the transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime

from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.context.search import lexical, scope_clause, terms
from ttakkari.knowledge_models import (
    KnowledgeAssertion as A,
)
from ttakkari.knowledge_models import (
    KnowledgeAssertionEvidence as AE,
)
from ttakkari.knowledge_models import (
    KnowledgeEvidence as E,
)
from ttakkari.knowledge_models import (
    KnowledgeReviewEvent as V,
)
from ttakkari.knowledge_models import (
    KnowledgeRevision as R,
)
from ttakkari.knowledge_models import (
    KnowledgeUnit as U,
)
from ttakkari.models import CtxEntity, CtxSource, utcnow

PARSER = "text-exact-v1"


class KnowledgeError(ValueError):
    def __init__(self, code: str, status: int = 422):
        self.code, self.status = code, status
        super().__init__(code)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def revision_out(r: R) -> dict:
    return {
        "id": str(r.id),
        "source_key": r.source_key,
        "sha256": r.sha256,
        "parser_version": r.parser_version,
        "scope": r.scope,
        "sensitivity": r.sensitivity,
        "locator": r.locator,
        "active": r.active,
        "origin": r.origin,
        "source_id": str(r.source_id) if r.source_id else None,
        "created_at": r.created_at.isoformat(),
    }


def unit_out(u: U, r: R) -> dict:
    return {
        "id": str(u.id),
        "revision_id": str(r.id),
        "source_key": r.source_key,
        "scope": r.scope,
        "body": u.body,
        "sha256": u.sha256,
        "stable_key": u.stable_key,
        "locator": u.locator,
        "source_offset": u.source_offset,
        "provenance": u.provenance,
        "speaker": u.speaker,
        "active_branch": u.active_branch,
        "outcome": u.outcome,
        "reason": u.reason,
        "origin": r.origin,
        "revision_sha256": r.sha256,
        "event_time": u.event_time.isoformat() if u.event_time else None,
    }


def assertion_out(a: A) -> dict:
    return {
        k: (
            str(v)
            if isinstance(v, uuid.UUID)
            else v.isoformat()
            if isinstance(v, datetime)
            else v
        )
        for k in (
            "id",
            "subject",
            "predicate",
            "object",
            "scope",
            "modality",
            "polarity",
            "state",
            "review_level",
            "version",
            "valid_from",
            "valid_to",
            "created_at",
            "updated_at",
            "supersedes_id",
        )
        for v in [getattr(a, k)]
    }


def visible(scope: str, *, current: bool = True):
    # Source restrictions are enforced at query time, even before reindexing.
    rules = [
        scope_clause(R.scope, scope),
        or_(
            R.source_id.is_(None),
            (CtxSource.enabled.is_(True) & scope_clause(CtxSource.scope, scope)),
        ),
        or_(
            R.origin.not_in(("indexed_document", "source_bound")),
            R.source_id.is_not(None),
        ),
    ]
    if current:
        rules.append(R.active.is_(True))
    return rules


async def _source_lock(db: AsyncSession, key: str) -> None:
    # Serializes import identity and active pointer updates across processes.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": "knowledge:" + key},
    )


async def import_text(
    db: AsyncSession,
    *,
    source_key: str,
    body: str,
    scope: str = "personal",
    source_id: uuid.UUID | None = None,
    locator: str | None = None,
    sensitivity: str = "normal",
    origin: str = "text",
    speaker: str = "document",
    event_time: datetime | None = None,
    parser_version: str = PARSER,
) -> R:
    if "\0" in body:
        raise KnowledgeError("unsupported_nul_in_source")
    if source_id and origin == "text":
        origin = "source_bound"
    await _source_lock(db, source_key)
    previous = await db.scalar(select(R).where(R.source_key == source_key).limit(1))
    if previous and (previous.scope != scope or previous.source_id != source_id):
        raise KnowledgeError("source_identity_conflict", 409)
    if source_id:
        source = await db.get(CtxSource, source_id)
        if not source or source.scope != scope:
            raise KnowledgeError("source_scope_mismatch")
    sha = digest(body)
    existing = await db.scalar(
        select(R).where(
            R.source_key == source_key,
            R.sha256 == sha,
            R.parser_version == parser_version,
        )
    )
    if existing:
        return existing  # Old revision replay never moves the active pointer backwards.
    await db.execute(
        update(R)
        .where(R.source_key == source_key, R.active.is_(True))
        .values(active=False)
    )
    revision = R(
        source_key=source_key,
        sha256=sha,
        parser_version=parser_version,
        scope=scope,
        sensitivity=sensitivity,
        source_id=source_id,
        locator=locator,
        origin=origin,
    )
    db.add(revision)
    await db.flush()
    # Exact slices retain whitespace. Paragraph boundaries reduce revision churn.
    occurrence: dict[str, int] = {}
    parts = re.findall(r".*?(?:\n\n|\Z)", body, flags=re.DOTALL)
    segments = [
        part[i : i + 1600] for part in parts if part for i in range(0, len(part), 1600)
    ] or [""]
    offset = 0
    for segment in segments:
        h = digest(segment)
        n = occurrence.get(h, 0)
        occurrence[h] = n + 1
        external = f"{h}:{n}"
        db.add(
            U(
                revision_id=revision.id,
                external_key=external,
                stable_key=digest(source_key + "\0" + external),
                locator=locator or source_key,
                body=segment,
                sha256=h,
                source_offset=offset,
                speaker=speaker,
                event_time=event_time,
                active_branch=True,
                outcome="pending_extraction" if segment else "no_message",
                reason=None if segment else "empty_text",
            )
        )
        offset += len(segment)
    await db.flush()
    return revision


async def get_unit(
    db: AsyncSession, uid: uuid.UUID, scope: str = "all", *, current: bool = False
):
    row = (
        await db.execute(
            select(U, R)
            .join(R, U.revision_id == R.id)
            .outerjoin(CtxSource, CtxSource.id == R.source_id)
            .where(U.id == uid, *visible(scope, current=current))
        )
    ).first()
    if not row:
        raise KnowledgeError("unit_not_found", 404)
    return row


async def _entity(db: AsyncSession, name: str, scope: str):
    alias = (
        func.jsonb_array_elements_text(CtxEntity.aliases)
        .table_valued("value")
        .alias("ka")
    )
    match = (
        select(1)
        .select_from(alias)
        .where(func.lower(alias.c.value) == name.casefold())
        .exists()
    )
    rows = list(
        await db.scalars(
            select(CtxEntity)
            .where(
                scope_clause(CtxEntity.scope, scope),
                or_(func.lower(CtxEntity.name) == name.casefold(), match),
            )
            .limit(2)
        )
    )
    return rows[0].id if len(rows) == 1 else None  # Ambiguity never merges identities.


async def add_assertion(
    db: AsyncSession,
    *,
    unit_id: uuid.UUID,
    start: int,
    end: int,
    quote: str,
    subject: str,
    predicate: str,
    object: str,
    modality: str = "fact",
    polarity: bool = True,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    scope: str = "all",
) -> A:
    unit, revision = await get_unit(db, unit_id, scope)
    if not 0 <= start < end <= len(unit.body) or unit.body[start:end] != quote:
        raise KnowledgeError("evidence_span_mismatch")
    if valid_from and valid_to and valid_from >= valid_to:
        raise KnowledgeError("invalid_valid_interval")
    if not subject.strip() or not predicate.strip() or not object.strip():
        raise KnowledgeError("empty_assertion")
    normalized = [
        subject.strip(),
        re.sub(r"\s+", "_", predicate.strip().lower()),
        object.strip(),
    ]
    # Stable across identical reimports; retracted fingerprints are never resurrected.
    fingerprint = digest(
        json.dumps(
            [
                unit.stable_key,
                *normalized,
                modality,
                polarity,
                valid_from.isoformat() if valid_from else None,
                valid_to.isoformat() if valid_to else None,
            ],
            ensure_ascii=False,
        )
    )
    await _source_lock(db, "assertion:" + fingerprint)
    a = await db.scalar(select(A).where(A.fingerprint == fingerprint))
    if not a:
        a = A(
            fingerprint=fingerprint,
            subject=normalized[0],
            predicate=normalized[1],
            object=normalized[2],
            scope=revision.scope,
            modality=modality,
            polarity=polarity,
            valid_from=valid_from,
            valid_to=valid_to,
            subject_entity_id=await _entity(db, normalized[0], revision.scope),
            object_entity_id=await _entity(db, normalized[2], revision.scope),
        )
        db.add(a)
        await db.flush()
        db.add(
            V(
                assertion_id=a.id,
                version=1,
                actor="system",
                state="candidate",
                review_level="candidate",
                reason="evidence_bound_candidate",
            )
        )
    evidence = await db.scalar(
        select(E).where(E.unit_id == unit.id, E.start == start, E.end == end)
    )
    if not evidence:
        evidence = E(unit_id=unit.id, start=start, end=end, sha256=digest(quote))
        db.add(evidence)
        await db.flush()
    link = await db.get(AE, (a.id, evidence.id))
    if not link:
        db.add(AE(assertion_id=a.id, evidence_id=evidence.id, stance="supports"))
    await db.flush()
    return a


async def detail(db: AsyncSession, aid: uuid.UUID, scope: str = "all") -> dict:
    a = await db.get(A, aid)
    if not a or (scope != "all" and a.scope not in (scope, "shared")):
        raise KnowledgeError("assertion_not_found", 404)
    evidence = []
    rows = await db.execute(
        select(E, U, R)
        .join(AE, AE.evidence_id == E.id)
        .join(U, U.id == E.unit_id)
        .join(R, R.id == U.revision_id)
        .outerjoin(CtxSource, CtxSource.id == R.source_id)
        .where(AE.assertion_id == a.id, *visible(scope, current=False))
    )
    for e, u, r in rows:
        evidence.append(
            {
                **unit_out(u, r),
                "evidence_id": str(e.id),
                "start": e.start,
                "end": e.end,
                "quote": u.body[e.start : e.end],
                "quote_sha256": e.sha256,
            }
        )
    if not evidence:
        raise KnowledgeError("assertion_not_found", 404)
    history = list(
        await db.scalars(select(V).where(V.assertion_id == a.id).order_by(V.version))
    )
    return {
        **assertion_out(a),
        "evidence": evidence,
        "history": [
            {
                "version": v.version,
                "actor": v.actor,
                "state": v.state,
                "review_level": v.review_level,
                "reason": v.reason,
                "recorded_at": v.created_at.isoformat(),
            }
            for v in history
        ],
    }


async def review(
    db: AsyncSession,
    aid: uuid.UUID,
    *,
    expected_version: int,
    state: str,
    review_level: str | None = None,
    reason: str,
    actor: str = "user",
    scope: str = "all",
) -> A:
    await detail(
        db, aid, scope
    )  # Evidence visibility is checked before locking/mutation.
    a = await db.scalar(
        select(A)
        .where(A.id == aid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if a.version != expected_version:
        raise KnowledgeError("stale_assertion_version", 409)
    level = review_level or a.review_level
    if actor != "user" and level == "user_confirmed":
        raise KnowledgeError("user_confirmation_required", 403)
    if state == "active" and level == "candidate":
        raise KnowledgeError("review_required")
    if a.state in ("retracted", "superseded") and state != a.state:
        raise KnowledgeError("retracted_assertion_requires_new_correction", 409)
    a.version += 1
    a.state, a.review_level, a.updated_at = state, level, utcnow()
    db.add(
        V(
            assertion_id=a.id,
            version=a.version,
            actor=actor,
            state=state,
            review_level=level,
            reason=reason,
        )
    )
    await db.flush()
    return a


async def recall(
    db: AsyncSession,
    query: str,
    *,
    scope: str = "all",
    k: int = 8,
    as_of: datetime | None = None,
    hops: int = 1,
) -> dict:
    words = terms(query)
    if not words:
        return {"assertions": [], "evidence": [], "scope": scope}
    pred, score = lexical(U.body, query, words)
    ranked = (
        select(
            U.id.label("unit_id"),
            score.label("score"),
            func.row_number()
            .over(
                partition_by=func.coalesce(R.locator, R.source_key),
                order_by=(score.desc(), U.id),
            )
            .label("rank"),
        )
        .join(R, R.id == U.revision_id)
        .outerjoin(CtxSource, CtxSource.id == R.source_id)
        .where(
            *visible(scope),
            U.outcome == "pending_extraction",
            U.active_branch.is_not(False),
            pred,
        )
    ).subquery()
    rows = (
        await db.execute(
            select(U, R, ranked.c.score)
            .join(R, R.id == U.revision_id)
            .join(ranked, ranked.c.unit_id == U.id)
            .where(ranked.c.rank == 1)
            .order_by(ranked.c.score.desc(), U.id)
            .limit(k)
        )
    ).all()
    evidence = [{**unit_out(u, r), "score": float(s)} for u, r, s in rows]
    # A raw quote must carry known corrections too, or an agent can relearn a rejected claim.
    annotations = {}
    if rows:
        annotated = await db.execute(
            select(E.unit_id, A)
            .join(AE, AE.assertion_id == A.id)
            .join(E, E.id == AE.evidence_id)
            .where(
                E.unit_id.in_([u.id for u, _r, _s in rows]),
                scope_clause(A.scope, scope),
            )
        )
        for uid, a in annotated:
            annotations.setdefault(str(uid), {})[str(a.id)] = assertion_out(a)
    for item in evidence:
        item["assertion_annotations"] = list(annotations.get(item["id"], {}).values())
    at = as_of or utcnow()
    anchors = set(words)
    found, visited = [], set()
    anchor_paths = {}
    hop_count = min(max(hops, 1), 2)
    truncated = False
    active_ids = set(
        await db.scalars(
            select(R.id)
            .outerjoin(CtxSource, CtxSource.id == R.source_id)
            .where(*visible(scope))
        )
    )
    for hop in range(hop_count):
        if not anchors:
            break
        truncated |= len(anchors) > 60
        anchors = set(sorted(anchors)[:60])
        subject_match = func.lower(A.subject).in_(anchors)
        object_match = func.lower(A.object).in_(anchors)
        if hop == 0:
            predicate, relevance = lexical(
                func.concat_ws(" ", A.subject, A.predicate, A.object), query, words
            )
            exact = or_(
                func.lower(A.subject) == query.strip().casefold(),
                func.lower(A.object) == query.strip().casefold(),
            )
            relevance = relevance + case((exact, 1.0), else_=0.0)
        else:
            # A shared substring is not an edge. Expansion follows complete endpoint labels.
            predicate = or_(subject_match, object_match)
            relevance = case((subject_match, 2.0), else_=1.0)
        # Filter by question and visibility BEFORE applying the bounded candidate budget.
        statement = (
            select(A, relevance.label("relevance"))
            .join(AE, AE.assertion_id == A.id)
            .join(E, E.id == AE.evidence_id)
            .join(U, U.id == E.unit_id)
            .join(R, R.id == U.revision_id)
            .outerjoin(CtxSource, CtxSource.id == R.source_id)
            .where(
                scope_clause(A.scope, scope),
                A.state == "active",
                A.review_level != "candidate",
                or_(A.valid_from.is_(None), A.valid_from <= at),
                or_(A.valid_to.is_(None), A.valid_to > at),
                *visible(scope),
                U.active_branch.is_(True),
                predicate,
                A.id.not_in(visited),
            )
            .distinct()
            .order_by(relevance.desc(), A.id)
            .limit(501)
        )
        candidates = (await db.execute(statement)).all()
        truncated |= len(candidates) > 500
        additions = set()
        next_paths = {}
        # First-hop hits cannot consume the entire explicitly requested two-hop budget.
        hop_limit = max(1, k // 2) if hop_count == 2 and hop == 0 else k - len(found)
        selected = 0
        for a, _score in candidates[:500]:
            if a.id in visited:
                continue
            d = await detail(db, a.id, scope)
            d["evidence"] = [
                e
                for e in d["evidence"]
                if uuid.UUID(e["revision_id"]) in active_ids
                and e["active_branch"] is True
            ]
            d.pop("history", None)
            if hop:
                matched = (
                    a.subject.casefold()
                    if a.subject.casefold() in anchors
                    else a.object.casefold()
                )
                via = anchor_paths[matched]
                d["retrieval"] = {
                    "hop": hop + 1,
                    "matched_entity": a.subject
                    if a.subject.casefold() == matched
                    else a.object,
                    "path_assertion_ids": [*via, str(a.id)],
                    "identity_match": "exact_endpoint_label",
                }
            else:
                d["retrieval"] = {
                    "hop": 1,
                    "matched_query": query,
                    "path_assertion_ids": [str(a.id)],
                }
            found.append(d)
            visited.add(a.id)
            additions.update([a.subject.casefold(), a.object.casefold()])
            for endpoint in (a.subject.casefold(), a.object.casefold()):
                next_paths.setdefault(endpoint, d["retrieval"]["path_assertion_ids"])
            selected += 1
            if selected >= hop_limit or len(found) >= k:
                break
        anchors = additions
        anchor_paths = next_paths
        if len(found) >= k:
            break
    return {
        "assertions": found,
        "evidence": evidence,
        "scope": scope,
        "as_of": at.isoformat(),
        "temporal_mode": "valid_time_with_current_review_state",
        "hops": hops,
        "candidate_budget_exceeded": truncated,
        "retrieval_path": "lexical+reviewed_evidence_graph",
    }


async def correct(
    db: AsyncSession,
    aid: uuid.UUID,
    *,
    expected_version: int,
    replacement: dict,
    reason: str,
    scope: str = "all",
    actor: str = "user",
) -> dict:
    old = await detail(db, aid, scope)
    await _source_lock(db, "correction:" + str(aid))
    original = await db.scalar(
        select(A)
        .where(A.id == aid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if original.version != expected_version:
        raise KnowledgeError("stale_assertion_version", 409)
    if original.state in ("retracted", "superseded"):
        raise KnowledgeError("assertion_already_closed", 409)
    new = await add_assertion(db, scope=scope, **replacement)
    if new.id == aid or new.scope != original.scope:
        raise KnowledgeError("invalid_correction_target")
    if new.supersedes_id and new.supersedes_id != aid:
        raise KnowledgeError("replacement_already_linked", 409)
    new.supersedes_id = aid
    await review(
        db,
        aid,
        expected_version=expected_version,
        state="superseded",
        reason=reason,
        actor=actor,
        scope=scope,
    )
    # Only the explicit authenticated user's correction is a confirmation.
    new = await review(
        db,
        new.id,
        expected_version=new.version,
        state="active",
        review_level="user_confirmed" if actor == "user" else "source_reviewed",
        reason=reason,
        actor=actor,
        scope=scope,
    )
    return {
        "original": {**old, "state": "superseded"},
        "replacement": assertion_out(new),
    }


async def impact(db: AsyncSession, aid: uuid.UUID, scope: str = "all") -> dict:
    d = await detail(db, aid, scope)
    descendants, frontier = [aid], [aid]
    for _ in range(16):
        rows = list(
            await db.scalars(
                select(A.id).where(
                    A.supersedes_id.in_(frontier), scope_clause(A.scope, scope)
                )
            )
        )
        frontier = [i for i in rows if i not in descendants]
        if not frontier:
            break
        descendants.extend(frontier)
    return {
        "assertions": [str(i) for i in descendants],
        "evidence_count": len(d["evidence"]),
        "source_unit_ids": sorted({e["id"] for e in d["evidence"]}),
        "original_deletion": "not_performed",
        "projection_policy": "retracted claims never serve as active facts",
    }
