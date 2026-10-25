"""Stage-2 ML triage through a real mission: decision fields reach the envelope, the KB receipt and the mission view."""
import numpy as np
import pytest
from collector_support import article_html, prose
from test_collection_missions import BASE, source_body
from test_collector_pipeline import env  # noqa: F401

from ttakkari.collector import runner, triage_ml
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker

pytestmark = pytest.mark.security

# The goal shares no term with the (Korean) document, so the lexical rule alone would hold it.
GOAL = "프로그래밍 언어 컴파일러 최적화"


async def register(client, auth, env):  # noqa: F811
    env.server.page("/document", article_html("직렬화 설계", [prose("ko", 33, 12) + " 직렬화 트랜잭션 설계 근거를 설명한다."]))
    url = env.server.url("/document")
    body = source_body(seeds=[url], max_pages=1)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201
    made = await client.post(BASE + "/missions", headers=auth, json={"goal": GOAL, "urls": [url]})
    assert made.status_code == 202, made.text
    return made.json()


def fake_ml(*, language="ko"):
    artifact = triage_ml.load_artifact()
    vec = np.random.default_rng(3).normal(size=len(artifact["embedding"]["mean"]))

    async def evaluate(goal, docs, *, embed=None):
        assert goal == GOAL
        return [{
            "version": triage_ml.VERSION, "status": "evaluated", "reason": "", "artifact_digest": artifact["digest"],
            "embedding": {"model": artifact["embedding"]["model"], "snapshot": artifact["embedding"]["snapshot"],
                          "snapshot_ok": True},
            "axes": triage_ml.score_document(artifact, vec.tolist(), vec.tolist(), language=language, snapshot_ok=True),
            "cost": {"batch_docs": len(docs), "batch_wall_ms": 1.0, "per_doc_ms": 1.0, "child_metrics": {}},
        } for _ in docs]

    return evaluate


async def test_validated_semantic_axis_rescues_a_lexical_hold_and_is_recorded_everywhere(client, auth, env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(triage_ml, "evaluate_documents", fake_ml())
    mission = await register(client, auth, env)
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    detail = (await client.get(BASE + "/missions/" + mission["id"], headers=auth)).json()
    item = detail["results"][0]
    assert item["status"] == "accepted", item  # recoverable review, not a hold
    assert "review:semantic_overrides_lexical_hold" in item["reasons"]
    tri = item["triage"]
    assert tri["status"] == "review_needed"
    assert tri["stage2"]["status"] == "evaluated" and tri["stage2"]["effects"] == ["rescued_from_lexical_hold"]
    assert tri["validation"]["overall"] == "partial"
    assert tri["axes"]["topic_relevance_semantic"]["validation"] == "validated"
    assert tri["axes"]["relevance_lexical"]["validation"] == "unvalidated"
    assert tri["axes"]["explanation_quality"]["status"] == "unmeasured"
    assert tri["precise_eval"]["state"] == "pending" and tri["precise_eval"]["engine"]["status"] == "unavailable"
    summary = detail["triage"]
    assert summary["precise_eval"]["pending"] == 1 and summary["precise_eval"]["skipped_cap"] == 0
    assert summary["precise_eval"]["engine"] == "unavailable" and isinstance(summary["precise_eval"]["engine_detail"], str)
    assert summary["ml"]["documents_by_status"] == {"evaluated": 1}
    assert summary["ml"]["axes_validation"]["topic_relevance_semantic"]["status"] == "validated"
    assert summary["ml"]["axes_validation"]["information_value"]["label_source"].startswith("FineWeb-C")
    assert detail["counts"]["review_needed"] == 1
    async with sessionmaker()() as db:
        from sqlalchemy import select

        from ttakkari.collector_models import CollectorItem

        row = await db.scalar(select(CollectorItem).where(CollectorItem.url.like("%/document")))
        quality = row.envelope["quality"]
        assert quality["relevance"]["model_id"].startswith("ttakkari/e5-heads-v1")
        assert "validation=validated" in quality["relevance"]["evidence"]
        assert quality["education"]["status"] == "measured" and quality["education"]["scale"] == "ordinal"
        assert row.envelope["extensions"]["collector:triage"]["stage2"]["effects"] == ["rescued_from_lexical_hold"]


async def test_precise_queue_cap_is_per_job_and_keeps_the_document(client, auth, env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(triage_ml, "evaluate_documents", fake_ml())
    monkeypatch.setattr(get_settings(), "collector_triage_precise_queue_cap", 0)
    mission = await register(client, auth, env)
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    detail = (await client.get(BASE + "/missions/" + mission["id"], headers=auth)).json()
    assert detail["results"][0]["status"] == "accepted"
    assert detail["results"][0]["triage"]["precise_eval"]["state"] == "skipped_cap"
    assert detail["triage"]["precise_eval"] == {**detail["triage"]["precise_eval"], "pending": 0, "skipped_cap": 1,
                                                  "cap_per_job": 0}


async def test_without_the_e5_model_the_rules_decide_and_the_gap_is_visible(client, auth, env):  # noqa: F811
    mission = await register(client, auth, env)  # test env uses the hash embedder: stage 2 is disabled
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    detail = (await client.get(BASE + "/missions/" + mission["id"], headers=auth)).json()
    item = detail["results"][0]
    assert item["status"] == "held" and "triage:irrelevant_to_goal" in item["reasons"]  # recoverable via unhold
    assert item["triage"]["stage2"]["status"] == "disabled"
    assert item["triage"]["validation"]["overall"] == "unvalidated"
    assert detail["triage"]["ml"]["documents_by_status"] == {"disabled": 1}
    assert detail["triage"]["precise_eval"]["pending"] == 0
