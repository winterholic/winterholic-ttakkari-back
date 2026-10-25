"""리드 결정 1: 근거 있는 L2/명시적 서술 관계는 active 로 자동 승격하고, 구조 유추 관계는 candidate 로 둔다.

- 순수 단위: 명시적 서술 패턴 추출기의 양성/음성 반례.
- 실 PostgreSQL: 파이프라인 전체를 지나 승격이 스키마 단일 진입점(activate_relation_with_evidence)으로만 일어나고,
  근거 없는 active 가 0건이며, 재실행해도 행이 늘지 않는다.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from test_kb_v3_integration_flow import _drain, _env, _register, _worker  # noqa: F401 - _env 는 autouse 픽스처

from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbPassage
from ttakkari.kb_ontology_models import KbEntity, KbRelation, KbRelationEvidence
from ttakkari.kb_pipeline.extract_relations import build_label_index, extract_explicit_relations
from ttakkari.kb_pipeline.alias_dictionary import load_seed_dictionary

EXPLICIT_DOC = """# 쿠버네티스(Kubernetes)

## 디플로이먼트(Deployment)

디플로이먼트(Deployment)는 파드와 레플리카셋에 대한 선언적 업데이트를 제공하는 워크로드 리소스이다. 디플로이먼트는 레플리카셋을 관리한다.

ReplicaSet manages Pod objects and keeps the replica count stable.

## 네트워크

인그레스는 서비스를 통해 트래픽을 전달한다.
"""


def _passage(body: str) -> KbPassage:
    return KbPassage(id=uuid.uuid4(), revision_id=uuid.uuid4(), ord=0, body=body, char_start=0, char_end=len(body), sha256="x")


def _index():
    return build_label_index(load_seed_dictionary().entities)


class TestExplicitRelationPatterns:
    """DB 를 쓰지 않는 순수 시험이라 conftest 의 DB 픽스처를 끈다."""

    @pytest.fixture(scope="session", autouse=True)
    def _schema_and_app(self):
        yield

    @pytest.fixture(autouse=True)
    def _clean(self):
        yield

    def test_korean_and_english_statements_yield_typed_relations(self):
        p = _passage("디플로이먼트는 레플리카셋을 관리한다. ReplicaSet manages Pod objects. StatefulSet depends on PersistentVolume.")
        result = extract_explicit_relations([p], _index())
        got = {(r.source_name, r.relation_type, r.target_name) for r in result.relations}
        assert ("Deployment", "manages", "ReplicaSet") in got
        assert ("ReplicaSet", "manages", "Pod") in got
        assert ("StatefulSet", "depends_on", "PersistentVolume") in got
        assert all(r.basis == "explicit" and r.status == "candidate" for r in result.relations)
        for r in result.relations:  # 인용은 본문의 정확한 슬라이스이고 오프셋이 함께 간다
            assert p.body[r.span_start:r.span_end] == r.evidence_quote

    @pytest.mark.parametrize("text", [
        "디플로이먼트는 레플리카셋을 관리하지 않는다.",  # 부정
        "Deployment does not manage ReplicaSet.",  # 부정
        "Deployment never manages ReplicaSet.",  # 부사가 끼면 인접 패턴이 아니다
        "디플로이먼트는 파드와 레플리카셋을 관리한다.",  # 두 개념이 이어진 목록: 인접 쌍이 아니다
        "디플로이먼트가 레플리카셋을 관리할 수 있다.",  # 가능성 서술
        "디플로이먼트. 레플리카셋을 관리한다.",  # 문장을 가로지른다
        "Deployment manages everything.",  # 끝점이 개념이 아니다
        "하위디플로이먼트는 레플리카셋을 관리한다.",  # 단어 중간 표기
    ])
    def test_negations_lists_and_cross_sentence_text_yield_nothing(self, text):
        assert extract_explicit_relations([_passage(text)], _index()).relations == []

    def test_same_entity_in_two_scripts_is_not_a_self_relation(self):
        assert extract_explicit_relations([_passage("디플로이먼트는 Deployment를 관리한다.")], _index()).relations == []


async def test_pipeline_promotes_verified_explicit_relations_and_keeps_structure_candidate(_env):  # noqa: F811
    res, job_id = await _register(EXPLICIT_DOC)
    await _drain(_worker())
    async with sessionmaker()() as db:
        names = {e.id: e.canonical_name for e in await db.scalars(select(KbEntity))}
        rels = list(await db.scalars(select(KbRelation)))
        active = {(names[r.source_entity_id], r.relation_type, names[r.target_entity_id]) for r in rels if r.status == "active"}
        assert ("Deployment", "manages", "ReplicaSet") in active
        assert ("ReplicaSet", "manages", "Pod") in active
        for r in rels:
            n_evidence = await db.scalar(
                select(func.count()).select_from(KbRelationEvidence).where(KbRelationEvidence.relation_id == r.id)
            )
            assert r.evidence_count == n_evidence, "evidence_count 는 실제 근거 행 수와 같다"
            if r.status == "active":
                assert n_evidence >= 1 and r.representative_evidence_id is not None
        # 구조(헤딩) 유추 관계는 승격되지 않는다: part_of 중 어떤 것도 문장 근거 없이 active 가 아니다
        structural = [r for r in rels if r.relation_type == "part_of"]
        assert structural, "헤딩 계층 유추 관계가 만들어져야 승격 제외를 검증할 수 있다"
        assert all(r.status == "candidate" for r in structural), [(names[r.source_entity_id], r.status) for r in structural]

    before = len(rels)
    # 같은 리비전의 extracting 을 다시 돌려도 관계·근거 행이 늘지 않는다(멱등)
    from ttakkari.kb_pipeline import retry_pipeline_job

    await retry_pipeline_job(sessionmaker(), job_id, "extracting")
    await _drain(_worker())
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(KbRelation)) == before
        assert await db.scalar(select(func.count()).select_from(KbRelation).where(KbRelation.status == "active")) == len(active)
