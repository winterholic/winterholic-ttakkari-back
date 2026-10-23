import asyncio
import json

import pytest
from pydantic import ValidationError


def test_studio_can_create_edit_and_reopen_project(tmp_path):
    from ttakkari.creative.service import ProjectInput, Scene, Studio

    studio = Studio(tmp_path)
    p = studio.create(ProjectInput(name="작은 신호", brief="밤에서 새벽으로"))
    assert len(p["scenes"]) == 3
    assert sum(s["duration"] for s in p["scenes"]) == 30
    assert p["timeline"]["visual"][1]["start"] == 10
    assert p["timeline"]["visual"][-1]["end"] == 30
    scene = Scene(**{**p["scenes"][0], "caption": "안녕, 세상"})
    changed = studio.edit(p["id"], p["revision"], scenes=[scene, *p["scenes"][1:]])
    assert Studio(tmp_path).get(p["id"])["scenes"][0]["caption"] == "안녕, 세상"
    with pytest.raises(ValueError, match="다른 변경"):
        studio.edit(p["id"], p["revision"], scenes=changed["scenes"])


def test_studio_rejects_outside_asset_and_invalid_timeline(tmp_path):
    from ttakkari.creative.service import ProjectInput, Scene, Studio

    studio = Studio(tmp_path)
    p = studio.create(ProjectInput(name="안전한 제작"))
    with pytest.raises(ValueError):
        studio.get("../../outside")
    with pytest.raises(ValidationError):
        Scene(duration=float("nan"))
    with pytest.raises(ValueError, match="자산"):
        studio.edit(p["id"], p["revision"], scenes=[Scene(asset_id="missing")])
    with pytest.raises(ValueError, match="240"):
        studio.edit(p["id"], p["revision"], scenes=[Scene(duration=60)] * 5)


def test_real_render_cache_and_failed_job_retry(tmp_path):
    from ttakkari.creative.render import inspect_media, scan
    from ttakkari.creative.service import ProjectInput, Studio

    studio = Studio(tmp_path)
    p = studio.create(ProjectInput(name="실제 렌더", duration=6))
    studio.demo(p["id"])
    job = studio.enqueue(p["id"])
    asyncio.run(scan(studio))
    p = studio.get(p["id"])
    done = p["render_jobs"][-1]
    assert done["id"] == job["id"] and done["status"] == "succeeded", done
    info = inspect_media(studio.path(p["id"]) / done["output"])
    assert 5.9 <= info["duration"] <= 6.2
    assert info["has_audio"] and info["has_video"]
    assert studio.enqueue(p["id"])["id"] == done["id"]
    # Reuse an imported clip for one scene; the other scene encodes are retained.
    clip = studio.add_asset(
        p["id"],
        studio.path(p["id"]) / done["output"],
        name="기존 영상",
        kind="video",
        provenance="test-render",
    )
    current = studio.get(p["id"])
    studio.edit(
        p["id"],
        current["revision"],
        scenes=[
            {**current["scenes"][0], "asset_id": clip["id"]},
            *current["scenes"][1:],
        ],
    )
    studio.enqueue(p["id"])
    asyncio.run(scan(studio))
    newer = studio.get(p["id"])["render_jobs"][-1]
    assert newer["status"] == "succeeded" and newer["cached_scenes"] == 2, newer
    # Failed attempts stay visible; enqueue with retry creates a separate trace.
    with studio.transaction(p["id"]) as data:
        data["render_jobs"][-1]["status"] = "failed"
        data["render_jobs"][-1]["error"] = "interrupted"
    retried = studio.enqueue(p["id"], retry=True)
    assert retried["id"] != done["id"]


def test_render_snapshot_does_not_change_with_later_scene_edits(tmp_path):
    from ttakkari.creative.service import ProjectInput, Studio

    studio = Studio(tmp_path)
    p = studio.create(ProjectInput(name="스냅샷"))
    studio.demo(p["id"])
    job = studio.enqueue(p["id"])
    current = studio.get(p["id"])
    changed = [{**s, "caption": "수정"} for s in current["scenes"]]
    studio.edit(p["id"], current["revision"], scenes=changed)
    assert (
        studio.get(p["id"])["render_jobs"][-1]["snapshot"]["scenes"][0]["caption"]
        != "수정"
    )
    assert json.dumps(job, ensure_ascii=False)


def test_four_minute_project_has_valid_scenes(tmp_path):
    from ttakkari.creative.service import ProjectInput, Studio

    p = Studio(tmp_path).create(ProjectInput(name="네 분", duration=240))
    assert sum(s["duration"] for s in p["scenes"]) == 240
    assert all(s["duration"] <= 60 for s in p["scenes"])


def test_disguised_playlist_cannot_read_arbitrary_local_files(tmp_path):
    from ttakkari.api.creative import _normalize

    target = tmp_path / "fake.mp4"
    target.write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n"
    )
    with pytest.raises(ValueError):
        _normalize(target, ".mp4")


def test_short_music_fails_visibly_and_interrupted_work_is_recoverable(tmp_path):
    from ttakkari.creative.render import scan
    from ttakkari.creative.service import ProjectInput, Studio

    studio = Studio(tmp_path)
    p = studio.create(ProjectInput(name="짧은 음원", duration=3))
    studio.demo(p["id"])
    p = studio.get(p["id"])
    studio.edit(
        p["id"], p["revision"], scenes=[{**s, "duration": 10} for s in p["scenes"]]
    )
    studio.enqueue(p["id"])
    asyncio.run(scan(studio))
    job = studio.get(p["id"])["render_jobs"][-1]
    assert job["status"] == "failed" and "음악이 영상보다 짧습니다" in job["error"]
    assert job["output"] is None and job["ended_at"]
    studio.enqueue(p["id"], retry=True)
    with studio.transaction(p["id"]) as data:
        data["render_jobs"][-1]["status"] = "running"
    asyncio.run(scan(studio))
    assert "이전 실행이 중단" in studio.get(p["id"])["render_jobs"][-1]["error"]


def test_another_renderer_ownership_is_respected(tmp_path):
    import fcntl

    from ttakkari.creative.render import scan
    from ttakkari.creative.service import ProjectInput, Studio

    studio = Studio(tmp_path)
    p = studio.create(ProjectInput(name="동시 실행 방지", duration=3))
    studio.demo(p["id"])
    studio.enqueue(p["id"])
    with (tmp_path / "renderer.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(scan(studio))
        assert studio.get(p["id"])["render_jobs"][-1]["status"] == "queued"


def test_unreadable_scene_image_fails_fast_instead_of_hanging_ffmpeg(tmp_path):
    import time

    from ttakkari.creative.render import scan
    from ttakkari.creative.service import ProjectInput, Studio

    studio = Studio(tmp_path)
    p = studio.create(ProjectInput(name="깨진 이미지", duration=6))
    studio.demo(p["id"])
    current = studio.get(p["id"])
    asset_id = current["scenes"][0]["asset_id"]
    asset = next(a for a in current["assets"] if a["id"] == asset_id)
    (studio.path(p["id"]) / asset["file"]).write_bytes(b"not an image")
    studio.enqueue(p["id"])
    started = time.monotonic()
    asyncio.run(scan(studio))
    job = studio.get(p["id"])["render_jobs"][-1]
    assert job["status"] == "failed" and "읽을 수 없습니다" in job["error"], job
    assert time.monotonic() - started < 30
