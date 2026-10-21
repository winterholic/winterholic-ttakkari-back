from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_runner import write_bundle

from ttakkari.kb_eval import core as e
from ttakkari.kb_eval import review_api


def test_mobile_api_skeleton_resume_and_human_route_remains_blocked(tmp_path):
    bundle = e.load_bundle(write_bundle(tmp_path))
    app = FastAPI()
    app.include_router(review_api.create_router(bundle, tmp_path / "review.json"))
    with TestClient(app) as client:
        first = client.get("/kb-eval/review/items/q1")
        assert first.status_code == 200
        item = first.json()
        assert item["question"]["id"] == "q1"
        assert item["sources"][0]["source_uri"].startswith("fixture://")
        event = {
            "id": "q1",
            "label_version": 0,
            "decision": "approve",
            "actor_kind": "agent",
            "actor": "test-agent",
            "at": "2026-10-22T00:00:00Z",
        }
        response = client.post("/kb-eval/review/events", json=event)
        assert response.status_code == 200
        assert client.get("/kb-eval/review/state").json()["cursor"] == "q1"
        assert client.post("/kb-eval/review/events", json=event).status_code == 409
        event.update(id="q2", actor_kind="human")
        assert client.post("/kb-eval/review/events", json=event).status_code == 501
        event.update(actor_kind="agent", unexpected_config=True)
        assert client.post("/kb-eval/review/events", json=event).status_code == 422


def test_review_version_conflict_is_atomic(tmp_path):
    event = {
        "id": "q1",
        "label_version": 0,
        "decision": "approve",
        "actor_kind": "agent",
        "actor": "test-agent",
        "at": "2026-10-22T00:00:00Z",
    }

    def update(_):
        try:
            e.review_update(tmp_path / "state.json", event)
            return "saved"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(update, range(8)))
    assert outcomes.count("saved") == 1
    assert outcomes.count("label_version_conflict") == 7
    assert len(e.review_update(tmp_path / "state.json", None)["history"]) == 1
