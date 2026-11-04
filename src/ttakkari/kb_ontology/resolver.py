"""결정적 엔터티 해소기 (KB3-301).

원칙: 이름·철자·임베딩이 비슷하다는 사실만으로 동일성을 확정하지 않는다. 비슷하면 '병합 후보'로 남긴다.

자동 연결(=기존 엔터티에 언급을 붙이는 것)은 아래 세 경우뿐이다.
  (a) Step 1: 정규화 키(norm_name) 정확 일치, 단 entity_kind 가 같고 도메인이 호환될 때
  (b) Step 2: 등록 별칭(kb_entity_aliases) 또는 시드 별칭 일치, 같은 kind·도메인 조건
  (c) 별칭 학습: 같은 패시지 안의 명시적 괄호 병기, 또는 패시지 원문에 실제로 나타나는 별칭
Step 3(철자·임베딩 유사도)은 병합 후보(kb_conflicts 'ambiguous_entity')만 만든다.
이름이 같아도 kind 나 도메인이 다르면(Swift 언어 vs SWIFT 금융망) 병합하지 않고 후보로 남긴다.

가시성: 신규 엔터티·충돌의 visibility 는 입력이 아니라 원천 문서 접근에서 파생한다
(work→work_only, 접근 정보 없음/미상→work_only, personal→global). work_only 언급은 global 엔터티의
별칭·카운트를 바꾸지 않는다.

멱등성: mention_count/document_count 는 증분 덧셈이 아니라 kb_document_entities 의 (entity, passage)
유일 행에서 다시 계산한다. 같은 revision/passage 를 다시 처리해도 행·별칭·충돌이 늘지 않는다.
"""

from __future__ import annotations

import enum
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.common.entity_norm import (
    cosine_similarity,
    jaro_winkler_similarity,
    norm_name,
    split_bilingual_label,
)
from ttakkari.kb_ontology.compat import (
    KNOWN_HOMONYMS,
    disambiguated_norm,
    is_domain_compatible,
    is_kind_compatible,
)
from ttakkari.kb_ontology.seed_aliases import (
    SeedEntity,
    ensure_seed_entity,
    find_seeds_by_norm,
)
from ttakkari.kb_ontology_models import (
    KbConflict,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
)
from ttakkari.kb_visibility import derive_derived_policy

logger = logging.getLogger(__name__)

__all__ = [
    "KNOWN_HOMONYMS",
    "EmbedFn",
    "EntityResolutionStep",
    "ResolutionResult",
    "derive_scope",
    "is_domain_compatible",
    "kb_embed_fn",
    "recount_entity",
    "resolve_entity",
]

# 후보 생성 임계값. 후보만 만들 뿐 자동 연결에 쓰이지 않는다. 실데이터로 보정하지 않은 초기값이다(확인 필요).
NAME_SIM_CANDIDATE = 0.85
COSINE_CANDIDATE = 0.90
MAX_SIMILARITY_CANDIDATES = 3
MAX_EMBED_CANDIDATES = 64
MAX_NAME_SCAN = 5000

# 괄호 안 내용이 별칭이 아니라 분류 꼬리표인 경우(예: "Python (programming language)")는 별칭으로 배우지 않는다.
_QUALIFIER_NORMS = {
    norm_name(w)
    for w in (
        "programming language", "language", "framework", "library", "software", "database",
        "tool", "platform", "프로그래밍 언어", "언어", "프레임워크", "라이브러리", "소프트웨어",
        "데이터베이스", "도구", "플랫폼",
    )
}

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


async def kb_embed_fn(texts: list[str]) -> list[list[float]]:
    """운영 KB 검색이 쓰는 임베딩 경로(ttakkari.kb_retrieval.embed_complete: 운영은 로컬 multilingual-e5-large)에 연결한 어댑터.

    resolve_entity(embed_fn=kb_embed_fn)처럼 명시적으로 넘길 때만 쓰인다. 기본값이 아닌 이유:
    E5 는 호출마다 자식 프로세스에서 모델을 로드하므로 언급 단위로 부르면 비싸다.
    파이프라인 단계가 배치로 묶어 호출하는 것을 전제로 한다.
    """
    from ttakkari import kb_retrieval

    return await kb_retrieval.embed_complete(texts, query=False)


class EntityResolutionStep(enum.IntEnum):
    """해소 단계 구분."""

    NEW_ENTITY = 0  # 일치 없음 → 신규 생성
    EXACT_NORM_NAME = 1  # 정규화 이름 일치로 자동 연결
    ALIAS_OR_PARENTHETICAL = 2  # 등록/시드 별칭 일치로 자동 연결
    SIMILARITY_CANDIDATE = 3  # 철자·임베딩 유사: 자동 연결 없이 신규 생성 + 병합 후보 기록
    AMBIGUOUS_ISOLATED = 4  # 이름은 같거나 겹치지만 kind/도메인/별칭 소유가 달라 격리 + 후보 기록


@dataclass
class ResolutionResult:
    """엔터티 해소 결과."""

    entity: KbEntity
    step: EntityResolutionStep
    created: bool
    learned_aliases: list[str] = field(default_factory=list)
    rejected_aliases: list[str] = field(default_factory=list)
    conflicts: list[KbConflict] = field(default_factory=list)
    note: str = ""

    @property
    def conflict(self) -> KbConflict | None:
        return self.conflicts[0] if self.conflicts else None


@dataclass(frozen=True)
class ResolutionScope:
    visibility: str
    origin_scope: str


@dataclass
class _Ctx:
    tenant_id: str
    scope: ResolutionScope
    entity_kind: str
    domain: str
    document_id: uuid.UUID | None
    revision_id: uuid.UUID | None
    passage_id: uuid.UUID | None
    evidence_id: uuid.UUID | None
    role: str
    salience: float

    @property
    def visibility(self) -> str:
        return self.scope.visibility


_UNSET: Any = object()


def derive_scope(sources: Sequence[Any]) -> ResolutionScope:
    """원천 문서 접근(KbDocumentAccess 또는 'work'/'personal' 문자열)에서 파생 객체의 visibility·origin_scope 결정.

    정책 구현은 kb_visibility.derive_derived_policy 한 곳이다(파이프라인·추출기·writer 와 공유).
    원천 중 하나라도 work_only, 없음, 미상이면 work_only 다.
    """
    origin_scope, visibility = derive_derived_policy(sources)
    return ResolutionScope(visibility=visibility, origin_scope=origin_scope)


async def _load_sources(
    db: AsyncSession, document_id: uuid.UUID | None, source_access: Any
) -> list[Any]:
    if source_access is _UNSET:
        if document_id is None:
            return []
        return [await db.get(KbDocumentAccess, document_id)]
    if source_access is None:
        return [None]
    if isinstance(source_access, (list, tuple, set, frozenset)):
        return list(source_access)
    return [source_access]


def _restrict_visible(stmt: Any, ctx: _Ctx) -> Any:
    """global 컨텍스트는 global 엔터티만, work 컨텍스트는 work_only 와 global 을 본다(fail-closed)."""
    if ctx.visibility == "global":
        return stmt.where(KbEntity.visibility == "global")
    return stmt.where(KbEntity.visibility.in_(("work_only", "global")))


def _base_norm(entity: KbEntity) -> str:
    return entity.norm_name.split("~", 1)[0]


def _lang(text: str) -> str | None:
    if text.isascii():
        return "en"
    return "ko" if any(0xAC00 <= ord(c) <= 0xD7A3 for c in text) else None


def _prefer_work(entities: list[KbEntity], ctx: _Ctx) -> list[KbEntity]:
    if ctx.visibility == "work_only":
        work = [e for e in entities if e.visibility == "work_only"]
        if work:
            return work
    return entities


async def resolve_entity(
    db: AsyncSession,
    raw_name: str,
    *,
    aliases: list[str] | None = None,
    entity_kind: str = "concept",
    domain: str = "general",
    summary: str = "",
    passage_text: str | None = None,
    embed_fn: EmbedFn | None = None,
    embedding: list[float] | None = None,
    candidate_embeddings: dict[uuid.UUID, list[float]] | None = None,
    tenant_id: str = "personal",
    source_access: Any = _UNSET,
    document_id: uuid.UUID | None = None,
    revision_id: uuid.UUID | None = None,
    passage_id: uuid.UUID | None = None,
    evidence_id: uuid.UUID | None = None,
    role: str = "mentioned",
    salience: float = 0.5,
) -> ResolutionResult:
    """언급 하나를 엔터티에 해소한다.

    source_access: 원천 문서 접근(KbDocumentAccess 등)이나 그 목록. 생략하면 document_id 로 kb_document_access 를
        조회하고, 접근 정보가 없으면 work_only(fail-closed)다. visibility 를 호출자가 직접 정하는 인자는 없다.
    aliases/passage_text: 호출자가 같은 패시지에서 뽑은 별칭 후보. 패시지 원문에 실제로 나타나고(passage_text)
        패시지·근거에 닻이 있을 때만 별칭으로 인정한다. raw_name 의 괄호 병기는 닻이 있으면 인정한다.
    embed_fn/embedding/candidate_embeddings: Step 3 후보 생성 전용. 운영 경로는 kb_embed_fn, 테스트는 주입 벡터.
    """
    if not raw_name or not raw_name.strip():
        raise ValueError("raw_name cannot be empty")

    cleaned = raw_name.strip()
    canonical_label, pieces = split_bilingual_label(cleaned)
    target_norm = norm_name(canonical_label)
    if not target_norm:
        raise ValueError("raw_name has no normalizable characters")

    from ttakkari.kb_ontology.write_lock import lock_identity_writes

    await lock_identity_writes(db, tenant_id)
    sources = await _load_sources(db, document_id, source_access)
    ctx = _Ctx(
        tenant_id=tenant_id,
        scope=derive_scope(sources),
        entity_kind=entity_kind,
        domain=domain,
        document_id=document_id,
        revision_id=revision_id,
        passage_id=passage_id,
        evidence_id=evidence_id,
        role=role,
        salience=salience,
    )

    anchored = passage_id is not None or evidence_id is not None
    has_bracket = "(" in cleaned
    # 같은 문자열 안의 병기 표기(괄호 병기)는 원문 근거가 있는 별칭 후보다.
    bracket_aliases = [p for p in pieces if has_bracket and norm_name(p) and norm_name(p) != target_norm]
    grounded_extra: list[str] = []
    rejected: list[str] = []
    if aliases:
        text_norm = norm_name(passage_text) if passage_text else ""
        for a in aliases:
            a_norm = norm_name(a)
            if not a_norm or a_norm == target_norm:
                continue
            if anchored and text_norm and a_norm in text_norm:
                grounded_extra.append(a)
            else:
                rejected.append(a.strip())
    learnable: list[str] = []
    if anchored:
        for a in [*bracket_aliases, *grounded_extra]:
            if norm_name(a) not in {norm_name(x) for x in learnable}:
                learnable.append(a.strip())
    elif bracket_aliases:
        rejected.extend(a.strip() for a in bracket_aliases)
    # 조회에는 이름 본문과 같은 문자열 안의 병기만 쓴다(닻 없는 외부 별칭은 조회에도 쓰지 않는다).
    piece_norms = [norm_name(a) for a in [*bracket_aliases, *grounded_extra]]

    collisions: list[tuple[KbEntity, str]] = []

    def _collide(entity: KbEntity, reason: str) -> None:
        if all(entity.id != e.id for e, _ in collisions):
            collisions.append((entity, reason))

    # -----------------------------------------------------------------
    # Step 1: 정규화 이름 정확 일치 (kind·도메인 호환 필수)
    # -----------------------------------------------------------------
    step1 = _restrict_visible(
        select(KbEntity).where(
            KbEntity.tenant_id == tenant_id,
            KbEntity.status == "active",
            or_(
                KbEntity.norm_name == target_norm,
                KbEntity.norm_name.startswith(target_norm + "~", autoescape=True),
            ),
        ),
        ctx,
    ).order_by(KbEntity.visibility.desc(), KbEntity.created_at, KbEntity.id)
    same_name = list((await db.execute(step1)).scalars().all())
    compatible: list[KbEntity] = []
    for cand in same_name:
        if not is_kind_compatible(cand.entity_kind, entity_kind):
            _collide(cand, f"same name '{target_norm}' but entity_kind {cand.entity_kind} != {entity_kind}")
        elif not is_domain_compatible(cand.domain, domain, target_norm):
            _collide(cand, f"same name '{target_norm}' but domain {cand.domain} incompatible with {domain}")
        else:
            compatible.append(cand)
    if compatible:
        exact_domain = [e for e in compatible if e.domain == domain]
        pool = _prefer_work(exact_domain or compatible, ctx)
        if len(pool) == 1:
            return await _finalize_match(
                db, ctx, pool[0], EntityResolutionStep.EXACT_NORM_NAME, learnable, rejected,
                "Exact norm_name matched",
            )
        for e in pool:
            _collide(e, f"{len(pool)} compatible entities share name '{target_norm}'")

    # -----------------------------------------------------------------
    # Step 2-A: 등록 별칭 일치 (kind·도메인 호환 필수). 이름 본문 일치를 병기 일치보다 우선한다.
    # -----------------------------------------------------------------
    query_norms = {target_norm, *piece_norms}
    step2 = _restrict_visible(
        select(KbEntity, KbEntityAlias.norm_alias)
        .join(KbEntityAlias, KbEntity.id == KbEntityAlias.entity_id)
        .where(
            KbEntity.tenant_id == tenant_id,
            KbEntity.status == "active",
            KbEntityAlias.norm_alias.in_(query_norms),
        ),
        ctx,
    ).order_by(KbEntity.visibility.desc(), KbEntity.created_at, KbEntity.id)
    by_entity: dict[uuid.UUID, tuple[KbEntity, set[str]]] = {}
    for entity, a_norm in (await db.execute(step2)).all():
        by_entity.setdefault(entity.id, (entity, set()))[1].add(a_norm)
    alias_compat: list[tuple[KbEntity, set[str]]] = []
    for entity, hit_norms in by_entity.values():
        if any(entity.id == c.id for c, _ in collisions):
            continue
        if not is_kind_compatible(entity.entity_kind, entity_kind):
            _collide(entity, f"alias match but entity_kind {entity.entity_kind} != {entity_kind}")
        elif not any(is_domain_compatible(entity.domain, domain, n) for n in hit_norms):
            _collide(entity, f"alias match but domain {entity.domain} incompatible with {domain}")
        else:
            alias_compat.append((entity, hit_norms))
    if alias_compat:
        by_name = [(e, h) for e, h in alias_compat if target_norm in h]
        tier = [e for e, _ in (by_name or alias_compat)]
        pool = _prefer_work(tier, ctx)
        if len(pool) == 1:
            return await _finalize_match(
                db, ctx, pool[0], EntityResolutionStep.ALIAS_OR_PARENTHETICAL, learnable, rejected,
                "Registered alias matched",
            )
        for e in pool:
            _collide(e, f"alias shared by {len(pool)} entities")

    # -----------------------------------------------------------------
    # Step 2-B: 시드 사전 일치 (한 표기가 둘 이상의 시드를 가리키면 확정하지 않는다)
    # -----------------------------------------------------------------
    seed_hit: SeedEntity | None = None
    seed_tiers = [[target_norm], sorted(set(piece_norms))]
    for tier_norms in seed_tiers:
        seeds: list[SeedEntity] = []
        for n in tier_norms:
            for s in find_seeds_by_norm(n):
                if s not in seeds:
                    seeds.append(s)
        if len(seeds) == 1:
            seed_hit = seeds[0]
            break
        if len(seeds) > 1:
            break
    if seed_hit is not None:
        hit_norm = next(
            (n for n in [target_norm, *piece_norms] if seed_hit in find_seeds_by_norm(n)), target_norm
        )
        kind_ok = is_kind_compatible(seed_hit.entity_kind, entity_kind)
        domain_ok = is_domain_compatible(seed_hit.domain, domain, hit_norm)
        seed_entity, seed_created = await ensure_seed_entity(
            db, seed_hit, tenant_id, ctx.visibility, ctx.scope.origin_scope
        )
        if kind_ok and domain_ok:
            result = await _finalize_match(
                db, ctx, seed_entity, EntityResolutionStep.ALIAS_OR_PARENTHETICAL, learnable, rejected,
                f"Seed dictionary matched on '{seed_hit.canonical_name}'",
            )
            result.created = seed_created
            return result
        reason = (
            f"seed '{seed_hit.canonical_name}' kind {seed_hit.entity_kind} != {entity_kind}"
            if not kind_ok
            else f"seed '{seed_hit.canonical_name}' domain {seed_hit.domain} incompatible with {domain}"
        )
        _collide(seed_entity, reason)

    # -----------------------------------------------------------------
    # Step 3: 철자·임베딩 유사도 → 병합 후보만 생성 (자동 연결 없음)
    # -----------------------------------------------------------------
    similar = await _similarity_candidates(
        db, ctx, canonical_label, target_norm, summary, embed_fn, embedding, candidate_embeddings,
        exclude_ids={e.id for e, _ in collisions},
    )

    # 복수형·대소문자·구두점만 다른 이름("Fluid input heights" / "Fluid input height")은 비슷한 이름이 아니라 같은 이름이다.
    # 정확히 하나일 때만 합치고, kind·도메인이 호환돼야 한다. 그 밖의 유사 이름은 계속 후보로만 남긴다.
    folded = _fold_inflection(target_norm)
    twins = [
        e for e, _ in similar
        if _fold_inflection(e.norm_name.split("~")[0]) == folded
        and is_kind_compatible(e.entity_kind, entity_kind) and is_domain_compatible(e.domain, domain, target_norm)
    ]
    if len(twins) == 1 and not collisions:
        return await _finalize_match(
            db, ctx, twins[0], EntityResolutionStep.EXACT_NORM_NAME, learnable, rejected,
            "Same name apart from plural form",
        )

    # -----------------------------------------------------------------
    # Step 4/신규: 일치가 없으면 독립 엔터티를 만들고 충돌·후보를 남긴다.
    # -----------------------------------------------------------------
    return await _create_entity_with_candidates(
        db, ctx, canonical_label, target_norm, summary, learnable, rejected, collisions, similar
    )


async def _similarity_candidates(
    db: AsyncSession,
    ctx: _Ctx,
    canonical_label: str,
    target_norm: str,
    summary: str,
    embed_fn: EmbedFn | None,
    embedding: list[float] | None,
    candidate_embeddings: dict[uuid.UUID, list[float]] | None,
    exclude_ids: set[uuid.UUID],
) -> list[tuple[KbEntity, str]]:
    """같은 kind·호환 도메인 엔터티 중 철자나 임베딩이 비슷한 것을 (엔터티, 사유)로 돌려준다. 연결은 하지 않는다."""
    stmt = _restrict_visible(
        select(KbEntity).where(
            KbEntity.tenant_id == ctx.tenant_id,
            KbEntity.status == "active",
            KbEntity.entity_kind == ctx.entity_kind,
        ),
        ctx,
    ).order_by(KbEntity.mention_count.desc(), KbEntity.id).limit(MAX_NAME_SCAN)
    pool = [
        c
        for c in (await db.execute(stmt)).scalars().all()
        if c.id not in exclude_ids
        and _base_norm(c) != target_norm
        and is_domain_compatible(c.domain, ctx.domain, target_norm)
    ]
    if not pool:
        return []
    alias_rows = (
        await db.execute(
            _restrict_visible(
                select(KbEntityAlias.entity_id, KbEntityAlias.norm_alias)
                .join(KbEntity, KbEntity.id == KbEntityAlias.entity_id)
                .where(
                    KbEntity.tenant_id == ctx.tenant_id,
                    KbEntity.status == "active",
                    KbEntity.entity_kind == ctx.entity_kind,
                ),
                ctx,
            )
        )
    ).all()
    names_of: dict[uuid.UUID, set[str]] = {c.id: {_base_norm(c)} for c in pool}
    for entity_id, a_norm in alias_rows:
        if entity_id in names_of:
            names_of[entity_id].add(a_norm)
    # 정본명뿐 아니라 등록 별칭과도 비교한다(예: 별칭 'spark' 와 새 표기 'Spark AR').
    name_sims = {
        c.id: max(jaro_winkler_similarity(target_norm, n) for n in names_of[c.id] if n != target_norm)
        if names_of[c.id] - {target_norm}
        else 0.0
        for c in pool
    }
    cos_sims: dict[uuid.UUID, float] = {}
    note_suffix = ""
    if embedding is not None and candidate_embeddings:
        for c in pool:
            vec = candidate_embeddings.get(c.id)
            if vec:
                cos_sims[c.id] = cosine_similarity(embedding, vec)
    elif embed_fn is not None:
        top = sorted(pool, key=lambda c: (-name_sims[c.id], -c.mention_count))[:MAX_EMBED_CANDIDATES]
        texts = [f"{canonical_label}. {summary}".strip()] + [f"{c.canonical_name}. {c.summary}".strip() for c in top]
        try:
            vectors = await embed_fn(texts)
            if len(vectors) != len(texts):
                raise ValueError("embedding count mismatch")
            for c, vec in zip(top, vectors[1:], strict=True):
                cos_sims[c.id] = cosine_similarity(vectors[0], vec)
        except Exception as exc:  # noqa: BLE001 - 임베딩 실패는 후보 생성만 약화시킨다. 철자 후보는 그대로 만든다.
            logger.warning("entity similarity embedding unavailable: %s", exc)
            note_suffix = " (embedding unavailable)"

    ranked: list[tuple[float, KbEntity, str]] = []
    for c in pool:
        ns = name_sims[c.id]
        cs = cos_sims.get(c.id, 0.0)
        if ns >= NAME_SIM_CANDIDATE or cs >= COSINE_CANDIDATE:
            ranked.append(
                (max(ns, cs), c, f"similarity candidate (name {ns:.2f}, cosine {cs:.2f}); not auto-merged{note_suffix}")
            )
    ranked.sort(key=lambda t: (-t[0], str(t[1].id)))
    return [(c, why) for _s, c, why in ranked[:MAX_SIMILARITY_CANDIDATES]]


async def _ensure_conflict(
    db: AsyncSession, ctx: _Ctx, existing: KbEntity, candidate: KbEntity, note: str
) -> KbConflict | None:
    """(existing, candidate) 쌍의 ambiguous_entity 충돌을 중복 없이 보장한다(순서 무관)."""
    if existing.id == candidate.id:
        return None
    found = (
        await db.execute(
            select(KbConflict).where(
                KbConflict.conflict_kind == "ambiguous_entity",
                or_(
                    (KbConflict.entity_id == existing.id) & (KbConflict.candidate_entity_id == candidate.id),
                    (KbConflict.entity_id == candidate.id) & (KbConflict.candidate_entity_id == existing.id),
                ),
            )
        )
    ).scalars().first()
    if found is not None:
        return found
    conflict = KbConflict(
        id=uuid.uuid4(),
        visibility=ctx.visibility,
        conflict_kind="ambiguous_entity",
        entity_id=existing.id,
        candidate_entity_id=candidate.id,
        status="open",
        resolution_note=note,
    )
    db.add(conflict)
    await db.flush()
    return conflict


def _alias_row(entity: KbEntity, text: str, norm: str, *, kind: str, source: str, lang: str | None) -> KbEntityAlias:
    return KbEntityAlias(
        id=uuid.uuid4(),
        entity_id=entity.id,
        alias=text,
        norm_alias=norm,
        norm_form="NFKC",
        lang=lang,
        alias_kind=kind,
        source=source,
    )


async def _filter_learnable_aliases(
    db: AsyncSession, ctx: _Ctx, entity: KbEntity, candidates: list[str], rejected: list[str]
) -> tuple[list[tuple[str, str]], list[tuple[KbEntity, str]]]:
    """학습할 별칭을 걸러낸다. 다른 엔터티가 이미 쓰는 표기, 다른 시드의 이름, 분류 꼬리표는 배우지 않는다.

    반환: ([(별칭, norm)], 병합 후보로 남길 [(다른 엔터티, 사유)])
    """
    keep: list[tuple[str, str]] = []
    others: list[tuple[KbEntity, str]] = []
    norms = {norm_name(a) for a in candidates}
    if not norms:
        return keep, others
    existing_norms = {
        r[0]
        for r in (
            await db.execute(select(KbEntityAlias.norm_alias).where(KbEntityAlias.entity_id == entity.id))
        ).all()
    }
    owner_rows = (
        await db.execute(
            select(KbEntity, KbEntityAlias.norm_alias)
            .join(KbEntityAlias, KbEntity.id == KbEntityAlias.entity_id)
            .where(
                KbEntity.tenant_id == ctx.tenant_id,
                KbEntity.visibility == entity.visibility,
                KbEntity.status == "active",
                KbEntity.id != entity.id,
                KbEntityAlias.norm_alias.in_(norms),
            )
        )
    ).all()
    owners: dict[str, list[KbEntity]] = {}
    for other, a_norm in owner_rows:
        owners.setdefault(a_norm, []).append(other)
    own_seed_norm = _base_norm(entity)
    for text in candidates:
        a_norm = norm_name(text)
        if a_norm in existing_norms or any(a_norm == k[1] for k in keep):
            continue
        if a_norm in _QUALIFIER_NORMS:
            rejected.append(text)
            continue
        if a_norm in owners:
            rejected.append(text)
            for other in owners[a_norm]:
                others.append((other, f"alias '{text}' is already owned by another entity"))
            continue
        foreign_seed = [s for s in find_seeds_by_norm(a_norm) if norm_name(s.canonical_name) != own_seed_norm]
        if foreign_seed:
            rejected.append(text)
            continue
        keep.append((text, a_norm))
    return keep, others


async def _finalize_match(
    db: AsyncSession,
    ctx: _Ctx,
    entity: KbEntity,
    step: EntityResolutionStep,
    learnable: list[str],
    rejected: list[str],
    note: str,
) -> ResolutionResult:
    """자동 연결 확정: 별칭 학습(조건부), (entity, passage) 연결, 카운트 재계산."""
    learned: list[str] = []
    conflicts: list[KbConflict] = []
    # work_only 언급은 global 엔터티의 별칭 사전을 바꾸지 않는다.
    polluting = ctx.visibility == "work_only" and entity.visibility == "global"
    if polluting:
        rejected = [*rejected, *learnable]
    elif learnable:
        keep, others = await _filter_learnable_aliases(db, ctx, entity, learnable, rejected)
        for text, a_norm in keep:
            db.add(_alias_row(entity, text, a_norm, kind="synonym", source="extracted", lang=_lang(text)))
            learned.append(text)
        for other, why in others:
            conflict = await _ensure_conflict(db, ctx, entity, other, why)
            if conflict is not None:
                conflicts.append(conflict)
        await db.flush()

    await _link_and_recount(db, ctx, entity)
    return ResolutionResult(
        entity=entity,
        step=step,
        created=False,
        learned_aliases=learned,
        rejected_aliases=rejected,
        conflicts=conflicts,
        note=note,
    )


def _fold_inflection(norm: str) -> str:
    """Normalized name with each English token reduced to a crude singular, for plural-only differences."""
    tokens = []
    for token in norm.split():
        if len(token) > 3 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
            token = token[:-1]
        tokens.append(token)
    return " ".join(tokens)


async def _create_entity_with_candidates(
    db: AsyncSession,
    ctx: _Ctx,
    canonical_label: str,
    target_norm: str,
    summary: str,
    learnable: list[str],
    rejected: list[str],
    collisions: list[tuple[KbEntity, str]],
    similar: list[tuple[KbEntity, str]],
) -> ResolutionResult:
    """독립 엔터티를 만들고 이름 충돌·유사 후보를 ambiguous_entity 로 남긴다."""
    key = await _free_slot_key(db, ctx, target_norm)
    entity = KbEntity(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        origin_scope=ctx.scope.origin_scope,
        visibility=ctx.visibility,
        canonical_name=canonical_label,
        norm_name=key,
        norm_form="NFKC",
        entity_kind=ctx.entity_kind,
        domain=ctx.domain,
        summary=summary,
        mention_count=0,
        document_count=0,
        confidence=0.7 if (collisions or similar) else 0.85,
        status="active",
    )
    db.add(entity)
    await db.flush()
    db.add(
        _alias_row(entity, canonical_label, target_norm, kind="canonical", source="extracted", lang=_lang(canonical_label))
    )
    await db.flush()

    keep, others = await _filter_learnable_aliases(db, ctx, entity, learnable, rejected)
    learned: list[str] = []
    for text, a_norm in keep:
        db.add(_alias_row(entity, text, a_norm, kind="synonym", source="extracted", lang=_lang(text)))
        learned.append(text)
    await db.flush()

    conflicts: list[KbConflict] = []
    for other, why in [*collisions, *similar, *others]:
        conflict = await _ensure_conflict(db, ctx, other, entity, why)
        if conflict is not None and all(conflict.id != c.id for c in conflicts):
            conflicts.append(conflict)

    await _link_and_recount(db, ctx, entity)
    if collisions or others:
        step = EntityResolutionStep.AMBIGUOUS_ISOLATED
    elif similar:
        step = EntityResolutionStep.SIMILARITY_CANDIDATE
    else:
        step = EntityResolutionStep.NEW_ENTITY
    note = "; ".join(why for _e, why in [*collisions, *similar, *others]) or "New entity created"
    return ResolutionResult(
        entity=entity,
        step=step,
        created=True,
        learned_aliases=learned,
        rejected_aliases=rejected,
        conflicts=conflicts,
        note=note,
    )


async def _free_slot_key(db: AsyncSession, ctx: _Ctx, target_norm: str) -> str:
    """kb_entities 유일 키 (tenant, visibility, kind, norm_name) 에서 비어 있는 norm_name 을 고른다."""
    for key in (target_norm, disambiguated_norm(target_norm, ctx.domain)):
        taken = (
            await db.execute(
                select(KbEntity.id).where(
                    KbEntity.tenant_id == ctx.tenant_id,
                    KbEntity.visibility == ctx.visibility,
                    KbEntity.entity_kind == ctx.entity_kind,
                    KbEntity.norm_name == key,
                )
            )
        ).first()
        if taken is None:
            return key
    return f"{disambiguated_norm(target_norm, ctx.domain)}~{uuid.uuid4().hex[:8]}"


async def _link_and_recount(db: AsyncSession, ctx: _Ctx, entity: KbEntity) -> None:
    """(entity, passage) 연결을 중복 없이 만들고 카운트를 유일 행 수로 다시 계산한다."""
    if ctx.passage_id is not None and ctx.document_id is not None and ctx.revision_id is not None:
        existing = (
            await db.execute(
                select(KbDocumentEntity.passage_id).where(
                    KbDocumentEntity.passage_id == ctx.passage_id,
                    KbDocumentEntity.entity_id == entity.id,
                )
            )
        ).first()
        if existing is None:
            db.add(
                KbDocumentEntity(
                    passage_id=ctx.passage_id,
                    entity_id=entity.id,
                    document_id=ctx.document_id,
                    revision_id=ctx.revision_id,
                    evidence_id=ctx.evidence_id,
                    role=ctx.role,
                    salience=ctx.salience,
                )
            )
            await db.flush()
    await recount_entity(db, entity)


async def recount_entity(db: AsyncSession, entity: KbEntity) -> None:
    """mention_count/document_count 를 kb_document_entities 의 유일 행에서 계산한다(증분 덧셈 금지).

    global 엔터티는 접근 행이 personal·global 인 문서에서 온 행만 센다. work 문서나 접근 정보가 없는 문서의 행은
    연결은 남아 있어도 global 카운트에 들어가지 않는다.
    """
    stmt = select(
        func.count(KbDocumentEntity.passage_id),
        func.count(func.distinct(KbDocumentEntity.document_id)),
    ).where(KbDocumentEntity.entity_id == entity.id)
    if entity.visibility == "global":
        stmt = stmt.join(
            KbDocumentAccess, KbDocumentAccess.document_id == KbDocumentEntity.document_id
        ).where(
            KbDocumentAccess.scope == "personal",
            KbDocumentAccess.origin_scope != "work",
            KbDocumentAccess.visibility == "global",
        )
    mentions, documents = (await db.execute(stmt)).one()
    entity.mention_count = int(mentions)
    entity.document_count = int(documents)
    await db.flush()
