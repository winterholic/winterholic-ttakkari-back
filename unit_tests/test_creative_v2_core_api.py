"""HTTP boundary without a DB/model/browser: authenticated plan and video upload."""

import asyncio
import subprocess

import httpx
import pytest
from fastapi import FastAPI
from test_creative_v2_core_integration import direction
from test_creative_v2_dispatch import intent, make_wav

from ttakkari.api import creative_v2 as api
from ttakkari.creative.render import ffmpeg
from ttakkari.creative.service import Studio
from ttakkari.creative.v2.plans import PlanStore
from ttakkari.creative.v2.style_dna import StyleStore
from ttakkari.security.auth import require_user


@pytest.fixture
def application(tmp_path, monkeypatch):
    plans = PlanStore(tmp_path / "plans")
    monkeypatch.setattr(api, "default_plan_store", lambda: plans)
    monkeypatch.setattr(
        api, "default_style_store", lambda: StyleStore(tmp_path / "styles")
    )
    monkeypatch.setattr(api, "default_studio", lambda: Studio(tmp_path / "studio"))
    app = FastAPI()
    app.include_router(api.router)
    return app, plans


async def test_reviewed_director_and_video_upload_follow_http_contract(
    application, tmp_path
):
    app, _plans = application
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/creative/v2/plans")).status_code == 401
        app.dependency_overrides[require_user] = lambda: "owner"
        response = await client.post(
            "/api/creative/v2/plans",
            json={
                "brief": "사진으로 영상",
                "intent": intent().model_dump(mode="json"),
                "director": direction(("generative_video",) * 3),
            },
        )
        assert response.status_code == 201, response.text
        plan = response.json()
        assert (
            plan["scenes"][0]["shots"][0]["layer_tools"]["visual"]
            == "skill.comfy_video_renderer"
        )
        video = tmp_path / "sample.mp4"
        await asyncio.to_thread(
            subprocess.run,
            [
                ffmpeg(),
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=320x180:r=24",
                "-t",
                "1",
                "-c:v",
                "libx264",
                str(video),
            ],
            check=True,
            capture_output=True,
        )
        path = f"/api/creative/v2/plans/{plan['id']}/production/assets"
        uploaded = await client.post(
            path,
            data={"kind": "scene", "scene_id": "i0"},
            files={"file": ("sample.mp4", video.read_bytes(), "video/mp4")},
        )
        assert uploaded.status_code == 201, uploaded.text
        assert uploaded.json()["kind"] == "video"
        malformed = await client.post(
            path,
            data={"kind": "scene", "scene_id": "i0"},
            files={"file": ("broken.mp4", b"not a video", "video/mp4")},
        )
        assert malformed.status_code == 422
        unknown = await client.post(
            "/api/creative/v2/plans",
            json={
                "brief": "사진",
                "intent": intent().model_dump(mode="json"),
                "director": {**direction(), "file_path": "/tmp/arbitrary"},
            },
        )
        assert unknown.status_code == 422
        for sid in ("i1", "i2"):
            result = await client.post(
                path,
                data={"kind": "scene", "scene_id": sid},
                files={"file": ("sample.mp4", video.read_bytes(), "video/mp4")},
            )
            assert result.status_code == 201
        wav = await asyncio.to_thread(
            make_wav, tmp_path / "music.wav", plan["scenes"][-1]["end_time"] + 1
        )
        result = await client.post(
            path,
            data={"kind": "music"},
            files={"file": ("music.wav", wav.read_bytes(), "audio/wav")},
        )
        assert result.status_code == 201
        base = f"/api/creative/v2/plans/{plan['id']}"
        assert (
            await client.post(base + "/approve", json={"revision": 1})
        ).status_code == 200
        assert (
            await client.post(base + "/production", json={"revision": 1})
        ).status_code == 201
        rendered = await client.post(base + "/production/render", json={"revision": 1})
        assert rendered.status_code == 202, rendered.text
        assert rendered.json()["state"] == "succeeded", rendered.text
        assert rendered.json()["quality_report"]["viewing_qa"]["verdict"] == "unknown"
        proposed = await client.post(
            base + "/production/repair-plan",
            json={
                "revision": 1,
                "defects": [
                    {
                        "scene_id": "i0",
                        "shot_id": "shot-0",
                        "code": "motion",
                        "layer": "motion",
                    }
                ],
            },
        )
        assert proposed.status_code == 200, proposed.text
        assert proposed.json()["invalidate"]["scene_ids"] == ["i0"]
        assert "scenes.i1" in proposed.json()["preserve"]["asset_keys"]
