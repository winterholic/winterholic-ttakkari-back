from fastapi import FastAPI
from fastapi.testclient import TestClient

from ttakkari.api import comfy_managed as api
from ttakkari.security.auth import require_user


def test_managed_comfy_api_exposes_only_registered_profiles_and_starts_owner_job(monkeypatch):
    class Settings:
        data_dir = None

    class Service:
        def __init__(self):
            self.started = []

        def profiles(self):
            return [{"id": "fixed-profile"}]

        def prepare(self, profile, *, prompt, seed):
            assert (profile, prompt, seed) == ("fixed-profile", "fox", 17)
            return {"task_id": "a" * 32, "profile": profile, "status": "starting",
                    "owner_attempt": "internal", "generation_token": "internal"}

        def start(self, task_id):
            self.started.append(task_id)

    service = Service()
    monkeypatch.setattr(api, "get_settings", lambda: Settings())
    monkeypatch.setattr(api, "default_controller", lambda _data_dir: service)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_user] = lambda: "owner"
    client = TestClient(app)

    assert client.get("/api/comfy/profiles").json() == [{"id": "fixed-profile"}]
    response = client.post("/api/comfy/jobs", json={"profile_id": "fixed-profile", "prompt": "fox", "seed": 17})
    assert response.status_code == 202
    assert response.json() == {"task_id": "a" * 32, "profile": "fixed-profile", "status": "starting",
                               "runner_job_id": None, "error": None, "result": {}}
    assert service.started == ["a" * 32]


def test_managed_comfy_api_rejects_raw_graph_and_unregistered_workflow(monkeypatch):
    class Settings:
        data_dir = None

    class Service:
        def prepare(self, profile, **kwargs):
            if profile != "fixed-profile":
                raise ValueError("unknown registered profile")
            raise AssertionError("raw graph input must fail request validation")

    monkeypatch.setattr(api, "get_settings", lambda: Settings())
    monkeypatch.setattr(api, "default_controller", lambda _data_dir: Service())
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_user] = lambda: "owner"
    client = TestClient(app)

    raw_graph = client.post("/api/comfy/jobs", json={"profile_id": "fixed-profile", "prompt": "fox", "seed": 1,
                                                     "workflow": {"arbitrary": "graph"}})
    assert raw_graph.status_code == 422
    unknown = client.post("/api/comfy/jobs", json={"profile_id": "user-controlled", "prompt": "fox", "seed": 1})
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "unknown registered profile"
