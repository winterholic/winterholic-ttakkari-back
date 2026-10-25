"""4단계 결정적 엔터티 해소기 (4-Stage Deterministic Entity Resolver).

설계 사양 (knowledge-library-refactor.md 2.4 2)):
1단계 (Step 1): 정규화 이름 정확 일치 (norm_name)
2단계 (Step 2): 등록된 한·영 별칭 사전(kb_entity_aliases) 및 시드 사전 일치
3단계 (Step 3): 문맥 임베딩 유사도 및 편집 거리 비교 (동일 entity_kind & domain 내)
4단계 (Step 4): 모호성 격리 (오병합 방지 불변 규칙) -> kb_conflicts('ambiguous_entity') 격리

증분 연결 및 가시성 경계 불변 조건:
- 새 문서의 언급을 기존 엔터티에 증분 연결(kb_document_entities).
- 새 별칭 학습은 근거(evidence_id 또는 괄호 병기 등 명시적 텍스트 출현)가 있을 때만 수행.
- visibility='work_only' 언급은 global 엔터티의 별칭 사전(kb_entity_aliases)이나 카운트를
  절대 오염시키지 않는다 (fail-closed 가시성 격리).
"""

from __future__ import annotations

import enum
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.common.entity_norm import (
    cosine_similarity,
    jaro_winkler_similarity,
    norm_name,
    split_bilingual_label,
)
from ttakkari.kb_ontology.seed_aliases import (
    SeedEntity,
    find_seed_by_norm,
)
from ttakkari.kb_ontology_models import (
    KbConflict,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
)

logger = logging.getLogger(__name__)


class EntityResolutionStep(enum.IntEnum):
    """해소 단계 구분."""

    NEW_ENTITY = 0  # 신규 생성
    EXACT_NORM_NAME = 1  # 1단계: 정규화 이름 일치
    ALIAS_OR_PARENTHETICAL = 2  # 2단계: 별칭 또는 괄호 병기 일치
    SIMILARITY_MATCH = 3  # 3단계: 문맥 임베딩 및 철자 유사도 일치
    AMBIGUOUS_ISOLATED = 4  # 4단계: 동음이의어 또는 경계 구간 모호성 격리


@dataclass
class ResolutionResult:
    """엔터티 해소 결과."""

    entity: KbEntity
    step: EntityResolutionStep
    created: bool
    learned_aliases: list[str] = field(default_factory=list)
    conflict: KbConflict | None = None
    note: str = ""


# 명백한 동음이의어(Homonyms) 키워드: 도메인이 다르면 절대로 동일 엔터티로 병합하지 않는다.
# key: norm_name, value: {도메인: 개념 설명}
KNOWN_HOMONYMS: dict[str, dict[str, str]] = {
    "python": {
        "dev": "Python 프로그래밍 언어",
        "science": "비단뱀 (파이톤 파충류)",
        "general": "비단뱀 또는 일반 용어",
    },
    "파이썬": {
        "dev": "Python 프로그래밍 언어",
        "science": "비단뱀 (파이톤 파충류)",
        "general": "비단뱀 또는 일반 용어",
    },
    "go": {
        "dev": "Go 프로그래밍 언어 (Golang)",
        "general": "동사 go 또는 바둑(Baduk/Go)",
    },
    "고": {
        "dev": "Go 프로그래밍 언어 (Golang)",
        "general": "동사 go 또는 바둑(Baduk/Go)",
    },
    "rust": {
        "dev": "Rust 시스템 프로그래밍 언어",
        "science": "녹, 철의 산화물 (Rust)",
        "general": "녹 또는 부식",
    },
    "러스트": {
        "dev": "Rust 시스템 프로그래밍 언어",
        "science": "녹, 철의 산화물 (Rust)",
        "general": "녹 또는 부식",
    },
    "service": {
        "infra": "쿠버네티스 Service 리소스",
        "business": "고객 서비스 또는 비즈니스 용역",
    },
    "서비스": {
        "infra": "쿠버네티스 Service 리소스",
        "business": "고객 서비스 또는 비즈니스 용역",
    },
    "java": {
        "dev": "Java 객체지향 언어",
        "general": "자바 섬 또는 자바 커피",
    },
    "자바": {
        "dev": "Java 객체지향 언어",
        "general": "자바 섬 또는 자바 커피",
    },
}


def is_domain_compatible(d1: str, d2: str, key_norm: str = "") -> bool:
    """두 도메인 간의 호환성 검사.

    동음이의어 사전(KNOWN_HOMONYMS)에 속한 경우 도메인이 다르면 명백히 비호환 처리.
    일반적인 경우 'general'은 모든 도메인과 호환되며, dev와 infra는 기술 공유를 허용한다.
    """
    if d1 == d2:
        return True

    # 동음이의어 검사: 등록된 동음이의어이고 도메인이 상충하면 비호환!
    if key_norm and key_norm in KNOWN_HOMONYMS:
        homonym_domains = KNOWN_HOMONYMS[key_norm]
        if d1 in homonym_domains and d2 in homonym_domains and d1 != d2:
            return False

    if d1 == "general" or d2 == "general":
        return True

    # 개발과 인프라는 기술 용어의 상호 호환을 허용
    if {d1, d2} <= {"dev", "infra"}:
        return True

    # 개발과 AI도 기술 용어 상호 호환을 허용
    return {d1, d2} <= {"dev", "ai"}


async def resolve_entity(
    db: AsyncSession,
    raw_name: str,
    *,
    aliases: list[str] | None = None,
    entity_kind: str = "concept",
    domain: str = "general",
    summary: str = "",
    embedding: list[float] | None = None,
    candidate_embeddings: dict[uuid.UUID, list[float]] | None = None,
    tenant_id: str = "personal",
    visibility: str = "global",
    origin_scope: str = "personal",
    document_id: uuid.UUID | None = None,
    revision_id: uuid.UUID | None = None,
    passage_id: uuid.UUID | None = None,
    evidence_id: uuid.UUID | None = None,
    role: str = "mentioned",
    salience: float = 0.5,
) -> ResolutionResult:
    """4단계 결정적 엔터티 해소 알고리즘 실행.

    오병합 방지, 멱등성, 증분 연결, 가시성 격리를 보장한다.
    """
    if not raw_name or not raw_name.strip():
        raise ValueError("raw_name cannot be empty")

    cleaned_input = raw_name.strip()

    # 1. 괄호 병기 라벨 분리 (예: '디플로이먼트(Deployment)' -> canonical: 'Deployment', aliases: ['디플로이먼트', 'Deployment'])
    canonical_label, extracted_aliases = split_bilingual_label(cleaned_input)
    target_norm = norm_name(canonical_label)

    # 추가 전달받은 별칭들과 괄호 분리 별칭들의 정규화 풀 구성
    all_input_aliases: list[str] = []
    if aliases:
        all_input_aliases.extend(aliases)
    all_input_aliases.extend(extracted_aliases)
    if cleaned_input not in all_input_aliases:
        all_input_aliases.append(cleaned_input)

    # 정규화된 별칭 집합 (주 이름 norm 제외)
    norm_alias_set: set[str] = {
        norm_name(a) for a in all_input_aliases if a and norm_name(a)
    }

    # -----------------------------------------------------------------
    # Step 1: 정규화 이름 일치 (norm_name)
    # -----------------------------------------------------------------
    step1_stmt = select(KbEntity).where(
        KbEntity.tenant_id == tenant_id,
        KbEntity.norm_name == target_norm,
        KbEntity.status == "active",
    )
    if visibility == "global":
        step1_stmt = step1_stmt.where(KbEntity.visibility == "global")
    else:
        # work_only 컨텍스트: work_only 우선 또는 global 조회
        step1_stmt = step1_stmt.where(
            KbEntity.visibility.in_(["work_only", "global"])
        ).order_by(
            # work_only 를 우선 선택
            KbEntity.visibility.desc()
        )

    res = await db.execute(step1_stmt)
    existing_entities = res.scalars().all()

    for candidate in existing_entities:
        # 동음이의어이고 도메인이 상충하는지 확인
        if target_norm in KNOWN_HOMONYMS and not is_domain_compatible(
            candidate.domain, domain, target_norm
        ):
            # 동음이의어이므로 Step 1에서 바로 합치지 않고 Step 4로 넘겨 격리한다.
            return await _isolate_ambiguous(
                db=db,
                existing_entity=candidate,
                canonical_name=canonical_label,
                norm_name_val=target_norm,
                entity_kind=entity_kind,
                domain=domain,
                summary=summary,
                tenant_id=tenant_id,
                visibility=visibility,
                origin_scope=origin_scope,
                aliases=all_input_aliases,
                document_id=document_id,
                revision_id=revision_id,
                passage_id=passage_id,
                evidence_id=evidence_id,
                role=role,
                salience=salience,
                note=f"Homonym collision on '{target_norm}': existing domain '{candidate.domain}' vs requested '{domain}'",
            )

        # 도메인이 호환되고 엔터티 종류가 호환되면 Step 1 일치!
        if is_domain_compatible(candidate.domain, domain, target_norm):
            return await _finalize_match(
                db=db,
                entity=candidate,
                step=EntityResolutionStep.EXACT_NORM_NAME,
                context_visibility=visibility,
                aliases=all_input_aliases,
                evidence_id=evidence_id,
                document_id=document_id,
                revision_id=revision_id,
                passage_id=passage_id,
                role=role,
                salience=salience,
                note="Exact norm_name matched",
            )

    # -----------------------------------------------------------------
    # Step 2: 등록된 별칭 사전 (kb_entity_aliases) 및 시드 사전 일치
    # -----------------------------------------------------------------
    all_query_norms = norm_alias_set | {target_norm}

    # 2-A: DB kb_entity_aliases 테이블 조회
    step2_stmt = (
        select(KbEntity, KbEntityAlias)
        .join(KbEntityAlias, KbEntity.id == KbEntityAlias.entity_id)
        .where(
            KbEntity.tenant_id == tenant_id,
            KbEntity.status == "active",
            KbEntityAlias.norm_alias.in_(all_query_norms),
        )
    )
    if visibility == "global":
        step2_stmt = step2_stmt.where(KbEntity.visibility == "global")
    else:
        step2_stmt = step2_stmt.where(
            KbEntity.visibility.in_(["work_only", "global"])
        ).order_by(KbEntity.visibility.desc())

    res2 = await db.execute(step2_stmt)
    alias_matches = res2.all()

    for matched_entity, matched_alias in alias_matches:
        alias_norm = matched_alias.norm_alias
        # 동음이의어 도메인 충돌 검사
        if alias_norm in KNOWN_HOMONYMS and not is_domain_compatible(
            matched_entity.domain, domain, alias_norm
        ):
            return await _isolate_ambiguous(
                db=db,
                existing_entity=matched_entity,
                canonical_name=canonical_label,
                norm_name_val=target_norm,
                entity_kind=entity_kind,
                domain=domain,
                summary=summary,
                tenant_id=tenant_id,
                visibility=visibility,
                origin_scope=origin_scope,
                aliases=all_input_aliases,
                document_id=document_id,
                revision_id=revision_id,
                passage_id=passage_id,
                evidence_id=evidence_id,
                role=role,
                salience=salience,
                note=f"Homonym alias collision on '{alias_norm}': existing domain '{matched_entity.domain}' vs requested '{domain}'",
            )

        if is_domain_compatible(matched_entity.domain, domain, alias_norm):
            return await _finalize_match(
                db=db,
                entity=matched_entity,
                step=EntityResolutionStep.ALIAS_OR_PARENTHETICAL,
                context_visibility=visibility,
                aliases=all_input_aliases,
                evidence_id=evidence_id,
                document_id=document_id,
                revision_id=revision_id,
                passage_id=passage_id,
                role=role,
                salience=salience,
                note=f"Alias matched via '{matched_alias.alias}'",
            )

    # 2-B: 시드 사전(seed_aliases) 조회
    for q_norm in all_query_norms:
        seed = find_seed_by_norm(q_norm)
        if seed:
            # 동음이의어 도메인 상충 검사
            if q_norm in KNOWN_HOMONYMS and not is_domain_compatible(
                seed.domain, domain, q_norm
            ):
                continue

            if is_domain_compatible(seed.domain, domain, q_norm):
                # 시드 엔터티를 DB에 생성하거나 기존 엔터티에 결속
                seed_entity, is_seed_created = await _ensure_seed_entity_in_db(
                    db=db,
                    seed=seed,
                    tenant_id=tenant_id,
                    visibility=visibility,
                    origin_scope=origin_scope,
                )
                return await _finalize_match(
                    db=db,
                    entity=seed_entity,
                    step=EntityResolutionStep.ALIAS_OR_PARENTHETICAL,
                    context_visibility=visibility,
                    aliases=all_input_aliases,
                    evidence_id=evidence_id,
                    document_id=document_id,
                    revision_id=revision_id,
                    passage_id=passage_id,
                    role=role,
                    salience=salience,
                    created=is_seed_created,
                    note=f"Seed dictionary matched on '{seed.canonical_name}'",
                )

    # -----------------------------------------------------------------
    # Step 3: 문맥 임베딩 & 이름 유사도 비교 (동일 entity_kind & domain 내)
    # -----------------------------------------------------------------
    # 동일 kind와 호환 domain의 활성 엔터티 후보 조회
    step3_stmt = select(KbEntity).where(
        KbEntity.tenant_id == tenant_id,
        KbEntity.status == "active",
        KbEntity.entity_kind == entity_kind,
    )
    if visibility == "global":
        step3_stmt = step3_stmt.where(KbEntity.visibility == "global")
    else:
        step3_stmt = step3_stmt.where(KbEntity.visibility.in_(["work_only", "global"]))

    res3 = await db.execute(step3_stmt)
    candidates = res3.scalars().all()

    best_candidate: KbEntity | None = None
    best_cos_sim = 0.0
    best_name_sim = 0.0

    for cand in candidates:
        if not is_domain_compatible(cand.domain, domain, target_norm):
            continue

        # 이름 유사도 (Jaro-Winkler)
        name_sim = jaro_winkler_similarity(target_norm, cand.norm_name)

        # 코사인 유사도
        cos_sim = 0.0
        if embedding is not None and candidate_embeddings:
            cand_vec = candidate_embeddings.get(cand.id)
            if cand_vec:
                cos_sim = cosine_similarity(embedding, cand_vec)

        if name_sim > best_name_sim or cos_sim > best_cos_sim:
            best_candidate = cand
            best_cos_sim = max(best_cos_sim, cos_sim)
            best_name_sim = max(best_name_sim, name_sim)

    # 설계 기준:
    # 코사인 유사도 >= 0.88 AND 이름 편집거리 >= 0.85 이면 동일 엔터티로 연결 (Step 3)
    if best_candidate and best_cos_sim >= 0.88 and best_name_sim >= 0.85:
        return await _finalize_match(
            db=db,
            entity=best_candidate,
            step=EntityResolutionStep.SIMILARITY_MATCH,
            context_visibility=visibility,
            aliases=all_input_aliases,
            evidence_id=evidence_id,
            document_id=document_id,
            revision_id=revision_id,
            passage_id=passage_id,
            role=role,
            salience=salience,
            note=f"Similarity matched (cos: {best_cos_sim:.2f}, name: {best_name_sim:.2f})",
        )

    # -----------------------------------------------------------------
    # Step 4: 모호성 격리 (오병합 방지 불변 규칙) 또는 신규 엔터티 생성
    # -----------------------------------------------------------------
    # 오병합 방지 불변 조건:
    # 1) 경계 유사도 구간(0.78 <= cos_sim < 0.88)
    # 2) 또는 이름 유사도만 높고(name_sim >= 0.85) 문맥 유사도가 미달인 경우
    # 절대 자동 병합하지 않고 ambiguous_entity 로 격리한다!
    if best_candidate and (
        (0.78 <= best_cos_sim < 0.88)
        or (best_name_sim >= 0.85 and best_cos_sim < 0.88)
    ):
        return await _isolate_ambiguous(
            db=db,
            existing_entity=best_candidate,
            canonical_name=canonical_label,
            norm_name_val=target_norm,
            entity_kind=entity_kind,
            domain=domain,
            summary=summary,
            tenant_id=tenant_id,
            visibility=visibility,
            origin_scope=origin_scope,
            aliases=all_input_aliases,
            document_id=document_id,
            revision_id=revision_id,
            passage_id=passage_id,
            evidence_id=evidence_id,
            role=role,
            salience=salience,
            note=f"Borderline similarity suspect (cos: {best_cos_sim:.2f}, name: {best_name_sim:.2f}) - isolated without auto-merge",
        )

    # 완전히 새로운 엔터티 생성
    return await _create_new_entity(
        db=db,
        canonical_name=canonical_label,
        norm_name_val=target_norm,
        entity_kind=entity_kind,
        domain=domain,
        summary=summary,
        tenant_id=tenant_id,
        visibility=visibility,
        origin_scope=origin_scope,
        aliases=all_input_aliases,
        document_id=document_id,
        revision_id=revision_id,
        passage_id=passage_id,
        evidence_id=evidence_id,
        role=role,
        salience=salience,
    )


async def _finalize_match(
    db: AsyncSession,
    entity: KbEntity,
    step: EntityResolutionStep,
    context_visibility: str,
    aliases: list[str],
    evidence_id: uuid.UUID | None,
    document_id: uuid.UUID | None,
    revision_id: uuid.UUID | None,
    passage_id: uuid.UUID | None,
    role: str,
    salience: float,
    note: str,
    created: bool = False,
) -> ResolutionResult:
    """기존 엔터티와 매칭 확정 및 증분 연결, 가시성 안전 규칙 적용."""
    learned_aliases: list[str] = []

    # 1. 가시성 오염 방지 규칙:
    # context_visibility == 'work_only' 이고 entity.visibility == 'global' 인 경우,
    # global 엔터티의 별칭 사전(kb_entity_aliases)이나 카운트를 절대 오염시키지 않는다!
    is_cross_scope_pollution_risk = (
        context_visibility == "work_only" and entity.visibility == "global"
    )

    if not is_cross_scope_pollution_risk:
        # 카운트 갱신
        entity.mention_count += 1
        entity.updated_at = datetime.now(UTC)

        # 새 별칭 학습: 근거(evidence_id)가 있거나 명시적인 별칭/유사도 매칭인 경우 추가
        if evidence_id is not None or step in (
            EntityResolutionStep.ALIAS_OR_PARENTHETICAL,
            EntityResolutionStep.SIMILARITY_MATCH,
        ):
            # 기존 등록된 별칭 조회
            stmt = select(KbEntityAlias.norm_alias).where(
                KbEntityAlias.entity_id == entity.id
            )
            res = await db.execute(stmt)
            existing_alias_norms = {r[0] for r in res.all()}
            existing_alias_norms.add(entity.norm_name)

            for a in aliases:
                a_norm = norm_name(a)
                if not a_norm or a_norm in existing_alias_norms:
                    continue
                alias_row = KbEntityAlias(
                    id=uuid.uuid4(),
                    entity_id=entity.id,
                    alias=a.strip(),
                    norm_alias=a_norm,
                    norm_form="NFKC",
                    lang="en" if a.isascii() else ("ko" if any(0xAC00 <= ord(c) <= 0xD7A3 for c in a) else None),
                    alias_kind="synonym",
                    source="extracted",
                )
                db.add(alias_row)
                existing_alias_norms.add(a_norm)
                learned_aliases.append(a.strip())

    # 2. 증분 연결: kb_document_entities 에 연결 (중복 방지)
    if passage_id is not None and document_id is not None and revision_id is not None:
        await _link_document_entity(
            db=db,
            passage_id=passage_id,
            entity_id=entity.id,
            document_id=document_id,
            revision_id=revision_id,
            evidence_id=evidence_id,
            role=role,
            salience=salience,
        )

    await db.flush()

    return ResolutionResult(
        entity=entity,
        step=step,
        created=created,
        learned_aliases=learned_aliases,
        conflict=None,
        note=note,
    )


async def _isolate_ambiguous(
    db: AsyncSession,
    existing_entity: KbEntity,
    canonical_name: str,
    norm_name_val: str,
    entity_kind: str,
    domain: str,
    summary: str,
    tenant_id: str,
    visibility: str,
    origin_scope: str,
    aliases: list[str],
    document_id: uuid.UUID | None,
    revision_id: uuid.UUID | None,
    passage_id: uuid.UUID | None,
    evidence_id: uuid.UUID | None,
    role: str,
    salience: float,
    note: str,
) -> ResolutionResult:
    """Step 4: 모호성/동음이의어 격리.

    기존 엔터티와 병합하지 않고 독립 엔터티를 신규 생성한 뒤,
    kb_conflicts(conflict_kind='ambiguous_entity') 에 격리 등록한다.
    """
    new_entity = KbEntity(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        origin_scope=origin_scope,
        visibility=visibility,
        canonical_name=canonical_name,
        norm_name=norm_name_val,
        norm_form="NFKC",
        entity_kind=entity_kind,
        domain=domain,
        summary=summary,
        mention_count=1,
        document_count=1 if document_id else 0,
        confidence=0.7,
        status="active",
    )
    db.add(new_entity)

    # 대표 별칭 추가
    db.add(
        KbEntityAlias(
            id=uuid.uuid4(),
            entity_id=new_entity.id,
            alias=canonical_name,
            norm_alias=norm_name_val,
            norm_form="NFKC",
            lang="en" if canonical_name.isascii() else None,
            alias_kind="canonical",
            source="extracted",
        )
    )

    # 충돌 격리 행 생성 (kb_conflicts)
    conflict = KbConflict(
        id=uuid.uuid4(),
        visibility=visibility,
        conflict_kind="ambiguous_entity",
        entity_id=existing_entity.id,
        candidate_entity_id=new_entity.id,
        status="open",
        resolution_note=note,
    )
    db.add(conflict)

    # 증분 연결
    if passage_id is not None and document_id is not None and revision_id is not None:
        await _link_document_entity(
            db=db,
            passage_id=passage_id,
            entity_id=new_entity.id,
            document_id=document_id,
            revision_id=revision_id,
            evidence_id=evidence_id,
            role=role,
            salience=salience,
        )

    await db.flush()
    logger.info(
        "Ambiguous entity isolated: '%s' vs '%s' (conflict_id=%s)",
        existing_entity.canonical_name,
        new_entity.canonical_name,
        conflict.id,
    )

    return ResolutionResult(
        entity=new_entity,
        step=EntityResolutionStep.AMBIGUOUS_ISOLATED,
        created=True,
        conflict=conflict,
        note=note,
    )


async def _create_new_entity(
    db: AsyncSession,
    canonical_name: str,
    norm_name_val: str,
    entity_kind: str,
    domain: str,
    summary: str,
    tenant_id: str,
    visibility: str,
    origin_scope: str,
    aliases: list[str],
    document_id: uuid.UUID | None,
    revision_id: uuid.UUID | None,
    passage_id: uuid.UUID | None,
    evidence_id: uuid.UUID | None,
    role: str,
    salience: float,
) -> ResolutionResult:
    """새로운 엔터티 및 별칭 등록."""
    new_entity = KbEntity(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        origin_scope=origin_scope,
        visibility=visibility,
        canonical_name=canonical_name,
        norm_name=norm_name_val,
        norm_form="NFKC",
        entity_kind=entity_kind,
        domain=domain,
        summary=summary,
        mention_count=1,
        document_count=1 if document_id else 0,
        confidence=0.85,
        status="active",
    )
    db.add(new_entity)

    # 기본 canonical 별칭 등록
    db.add(
        KbEntityAlias(
            id=uuid.uuid4(),
            entity_id=new_entity.id,
            alias=canonical_name,
            norm_alias=norm_name_val,
            norm_form="NFKC",
            lang="en" if canonical_name.isascii() else None,
            alias_kind="canonical",
            source="extracted",
        )
    )

    registered_norms = {norm_name_val}
    learned_aliases: list[str] = []

    for a in aliases:
        a_norm = norm_name(a)
        if not a_norm or a_norm in registered_norms:
            continue
        registered_norms.add(a_norm)
        db.add(
            KbEntityAlias(
                id=uuid.uuid4(),
                entity_id=new_entity.id,
                alias=a.strip(),
                norm_alias=a_norm,
                norm_form="NFKC",
                lang="en" if a.isascii() else ("ko" if any(0xAC00 <= ord(c) <= 0xD7A3 for c in a) else None),
                alias_kind="synonym",
                source="extracted",
            )
        )
        learned_aliases.append(a.strip())

    # 증분 연결
    if passage_id is not None and document_id is not None and revision_id is not None:
        await _link_document_entity(
            db=db,
            passage_id=passage_id,
            entity_id=new_entity.id,
            document_id=document_id,
            revision_id=revision_id,
            evidence_id=evidence_id,
            role=role,
            salience=salience,
        )

    await db.flush()

    return ResolutionResult(
        entity=new_entity,
        step=EntityResolutionStep.NEW_ENTITY,
        created=True,
        learned_aliases=learned_aliases,
        conflict=None,
        note="New entity created",
    )


async def _link_document_entity(
    db: AsyncSession,
    passage_id: uuid.UUID,
    entity_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    evidence_id: uuid.UUID | None,
    role: str,
    salience: float,
) -> None:
    """kb_document_entities 에 중복 없이 안전하게 증분 연결."""
    stmt = select(KbDocumentEntity).where(
        KbDocumentEntity.passage_id == passage_id,
        KbDocumentEntity.entity_id == entity_id,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        # 이미 연결되어 있음
        return

    doc_ent = KbDocumentEntity(
        passage_id=passage_id,
        entity_id=entity_id,
        document_id=document_id,
        revision_id=revision_id,
        evidence_id=evidence_id,
        role=role,
        salience=salience,
    )
    db.add(doc_ent)


async def _ensure_seed_entity_in_db(
    db: AsyncSession,
    seed: SeedEntity,
    tenant_id: str,
    visibility: str,
    origin_scope: str,
) -> tuple[KbEntity, bool]:
    """시드 엔터티가 DB에 없으면 생성하고, 이미 있으면 기존 엔터티 반환.

    Returns:
        (entity, is_created)
    """
    c_norm = norm_name(seed.canonical_name)

    stmt = select(KbEntity).where(
        KbEntity.tenant_id == tenant_id,
        KbEntity.visibility == visibility,
        KbEntity.norm_name == c_norm,
        KbEntity.status == "active",
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing, False

    # 신규 시드 엔터티 삽입
    entity = KbEntity(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        origin_scope=origin_scope,
        visibility=visibility,
        canonical_name=seed.canonical_name,
        norm_name=c_norm,
        norm_form="NFKC",
        entity_kind=seed.entity_kind,
        domain=seed.domain,
        summary=seed.summary,
        confidence=1.0,
        status="active",
    )
    db.add(entity)

    # canonical alias
    db.add(
        KbEntityAlias(
            id=uuid.uuid4(),
            entity_id=entity.id,
            alias=seed.canonical_name,
            norm_alias=c_norm,
            norm_form="NFKC",
            lang="en" if seed.canonical_name.isascii() else None,
            alias_kind="canonical",
            source="seed",
        )
    )

    registered_norms = {c_norm}
    for alias_text, lang, alias_kind in seed.aliases:
        a_norm = norm_name(alias_text)
        if not a_norm or a_norm in registered_norms:
            continue
        registered_norms.add(a_norm)
        db.add(
            KbEntityAlias(
                id=uuid.uuid4(),
                entity_id=entity.id,
                alias=alias_text,
                norm_alias=a_norm,
                norm_form="NFKC",
                lang=lang,
                alias_kind=alias_kind,
                source="seed",
            )
        )

    await db.flush()
    return entity, True
