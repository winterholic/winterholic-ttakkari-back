"""제작 기록(Production Records) 읽기 모델·경로 격리·API."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ttakkari.creative import director
from ttakkari.creative.records import (
    LabSource,
    RecordNotFound,
    RecordsService,
    record_from_g0,
    record_from_v1,
    safe_relative,
    summarize,
)
from ttakkari.creative.service import ProjectInput, Studio


def manifest(slug="demo-run", **over):
    base = {
        "schema_version": 1,
        "slug": slug,
        "title": "데모 실험",
        "kind": "motion_graphics",
        "summary": "요약",
        "status": "completed",
        "created_at": "2026-10-21T21:44:00+09:00",
        "engines": ["Remotion"],
        "duration_seconds": 16.0,
        "media": [
            {
                "id": "thumb",
                "kind": "image",
                "role": "keyframe",
                "label": "썸네일",
                "path": f"area/records/{slug}/thumb.png",
            },
            {
                "id": "clip",
                "kind": "video",
                "role": "result",
                "label": "영상",
                "path": "area/out/clip.mp4",
            },
            {
                "id": "doc",
                "kind": "document",
                "role": "document",
                "label": "문서",
                "path": "area/notes.md",
            },
        ],
        "thumbnail_media_id": "thumb",
        "metrics": [{"key": "mem", "label": "메모리", "value": None}],
    }
    return {**base, **over}


@pytest.fixture
def lab(tmp_path):
    root = tmp_path / "lab"
    folder = root / "area" / "records" / "demo-run"
    folder.mkdir(parents=True)
    (root / "area" / "out").mkdir()
    (folder / "thumb.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (root / "area" / "out" / "clip.mp4").write_bytes(b"0123456789abcdef")
    (root / "area" / "notes.md").write_text("# 노트\n")
    (folder / "record.json").write_text(json.dumps(manifest(), ensure_ascii=False))
    return root


def test_lab_manifest_is_normalized_with_source_and_missing_value(lab):
    records, skipped = LabSource(lab).load_all()
    assert skipped == []
    record = records[0]
    assert record.id == "lab-demo-run" and record.source == "lab"
    assert record.source_label == "lab 실험"
    assert record.metrics[0].value is None  # 근거 없는 값은 비워 둔다
    assert all(m.available for m in record.media)
    summary = summarize(record)
    assert summary.thumbnail_media_id == "thumb" and summary.verdict == "unreviewed"


def test_lab_ignores_server_owned_fields_in_manifest(lab):
    data = manifest(
        media=[
            {
                "id": "clip",
                "kind": "video",
                "role": "result",
                "label": "영상",
                "path": "area/out/clip.mp4",
                "url": "https://evil.example/x",
                "available": False,
            }
        ]
    )
    data["thumbnail_media_id"] = None
    (lab / "area/records/demo-run/record.json").write_text(json.dumps(data))
    record = LabSource(lab).get("demo-run")
    assert record.media[0].url is None and record.media[0].available is True


@pytest.mark.parametrize(
    "path",
    [
        "../outside.mp4",
        "area/../../outside.mp4",
        "/etc/passwd.mp4",
        "area\\x.mp4",
        "area/out/clip.mp4\x00.png",
        "",
    ],
)
def test_media_path_escape_is_rejected(lab, path):
    assert safe_relative(lab, path, "video") is None


def test_symlinked_file_and_directory_escape_are_rejected_after_resolution(
    lab, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mp4").write_bytes(b"secret")
    (lab / "area/out/link.mp4").symlink_to(outside / "secret.mp4")
    (lab / "area/linkdir").symlink_to(outside, target_is_directory=True)
    assert safe_relative(lab, "area/out/link.mp4", "video") is None
    assert safe_relative(lab, "area/linkdir/secret.mp4", "video") is None
    # 루트 안을 가리키는 링크는 허용된다.
    (lab / "area/out/inner.mp4").symlink_to(lab / "area/out/clip.mp4")
    assert safe_relative(lab, "area/out/inner.mp4", "video") is not None


def test_only_whitelisted_extensions_for_the_declared_kind_are_served(lab):
    (lab / "area/.env").write_text("SECRET=1")
    (lab / "area/run.py").write_text("print(1)")
    (lab / "area/page.html").write_text("<script>1</script>")
    for name in (".env", "run.py", "page.html"):
        assert safe_relative(lab, f"area/{name}", "document") is None
    # 종류와 확장자가 어긋나면 거부한다(영상으로 선언한 md).
    assert safe_relative(lab, "area/notes.md", "video") is None


def test_manifest_outside_root_or_invalid_is_skipped_not_fatal(lab, tmp_path):
    outside = tmp_path / "evil.json"
    outside.write_text(json.dumps(manifest("evil-run")))
    evil = lab / "area/records/evil-run"
    evil.mkdir()
    (evil / "record.json").symlink_to(outside)
    broken = lab / "area/records/broken-run"
    broken.mkdir()
    (broken / "record.json").write_text("{not json")
    mismatch = lab / "area/records/other-name"
    mismatch.mkdir()
    (mismatch / "record.json").write_text(json.dumps(manifest("different-slug")))
    bad_ref = lab / "area/records/bad-ref"
    bad_ref.mkdir()
    (bad_ref / "record.json").write_text(
        json.dumps(manifest("bad-ref", thumbnail_media_id="nope"))
    )
    records, skipped = LabSource(lab).load_all()
    assert [r.id for r in records] == ["lab-demo-run"]
    assert {s["path"].split("/")[-2] for s in skipped} == {
        "evil-run",
        "broken-run",
        "other-name",
        "bad-ref",
    }


def test_missing_media_file_is_flagged_unavailable_and_not_served(lab):
    (lab / "area/out/clip.mp4").unlink()
    service = LabSource(lab)
    record = service.get("demo-run")
    assert next(m for m in record.media if m.id == "clip").available is False
    assert record.warnings and summarize(record).complete_media is False
    with pytest.raises(RecordNotFound):
        service.media_file("demo-run", "clip")


def test_lab_media_file_mime_and_unknown_ids(lab):
    source = LabSource(lab)
    assert source.media_file("demo-run", "clip").mime == "video/mp4"
    assert source.media_file("demo-run", "doc").mime.startswith("text/plain")
    for bad in ("missing", "../clip"):
        with pytest.raises(RecordNotFound):
            source.media_file("demo-run", bad)
    for slug in ("../demo-run", "Demo-Run", "x"):
        with pytest.raises(RecordNotFound):
            source.get(slug)


def test_unconfigured_lab_root_lists_nothing(tmp_path):
    assert LabSource(None).load_all() == ([], [])
    assert LabSource(tmp_path / "missing").load_all() == ([], [])


def test_g0_production_is_normalized_with_attempt_output(tmp_path):
    store = director.DirectorStore(tmp_path / "productions")
    state = store.create_production("새벽 여행 영상\n두 번째 줄")
    pid = state["manifest"]["production_id"]
    art = store.put_artifact(
        pid, "existing.mp4", b"existing-media", media_type="video/mp4"
    )
    attempt = store.submit_attempt(
        pid, "shot-1", input_sha256=art["sha256"], idempotency_key="k1"
    )
    store.execute_attempt(pid, attempt["attempt_id"], renderer=lambda source: [source])
    record = record_from_g0(store.get_production(pid))
    assert record.id == f"g0-{pid}" and record.source == "g0"
    assert record.title == "새벽 여행 영상"
    assert record.status == "partial"  # shot-2는 아직 시도 없음
    assert record.review.verdict == "unreviewed"
    shot1 = next(s for s in record.shots if s.id == "shot-1")
    assert shot1.status == "ok" and shot1.output_media_id
    assert [m.kind for m in record.media] == [
        "video"
    ]  # 같은 해시의 .bin 출력은 원본 이름으로 합쳐진다
    stages = {s.key: s.status for s in record.stages}
    assert stages["brief"] == "done" and stages["review"] == "not_run"
    assert any(m.key == "peak_memory" and m.value is None for m in record.metrics)


def test_v1_project_is_normalized_and_missing_memory_is_unmeasured(tmp_path):
    studio = Studio(tmp_path / "studio")
    project = studio.create(ProjectInput(name="새벽 여행", duration=6))
    record = record_from_v1(studio.get(project["id"]))
    assert record.id == f"v1-{project['id']}" and record.source == "v1"
    assert record.status == "draft" and len(record.shots) == 3
    assert record.engines == ["ffmpeg"]
    assert next(m for m in record.metrics if m.key == "peak_rss").value is None


def test_service_merges_sources_and_filters(tmp_path, lab):
    studio = Studio(tmp_path / "studio")
    studio.create(ProjectInput(name="새벽 여행", duration=6))
    store = director.DirectorStore(tmp_path / "productions")
    store.create_production("브리프")
    service = RecordsService(director=store, studio=studio, lab_root=lab)
    everything = service.list()
    assert everything["counts"] == {"g0": 1, "v1": 1, "lab": 1}
    assert everything["total"] == 3
    assert service.list(source="lab")["total"] == 1
    assert service.list(kind="motion_graphics")["items"][0].id == "lab-demo-run"
    assert service.list(engine="remotion")["total"] == 1
    assert service.list(q="브리프")["total"] == 1
    assert "Remotion" in everything["engines"] and "ffmpeg" in everything["engines"]


def test_service_get_rejects_unknown_and_malformed_ids(tmp_path, lab):
    service = RecordsService(
        director=director.DirectorStore(tmp_path / "p"),
        studio=Studio(tmp_path / "s"),
        lab_root=lab,
    )
    for rid in ("lab-nope", "g0-not-a-uuid", "v1-" + "0" * 36, "zz-1"):
        with pytest.raises(FileNotFoundError):
            service.get(rid)


# --- API ---------------------------------------------------------------------------------


@pytest.fixture
def records_api(monkeypatch, tmp_path, lab):
    from ttakkari.api import creative as creative_api

    studio = Studio(tmp_path / "studio")
    store = director.DirectorStore(tmp_path / "productions")
    monkeypatch.setattr(
        creative_api,
        "_records",
        lambda: RecordsService(director=store, studio=studio, lab_root=lab),
    )
    return store, studio


async def test_records_endpoints_require_auth(client, records_api):
    assert (await client.get("/api/creative/records")).status_code == 401
    assert (await client.get("/api/creative/records/lab-demo-run")).status_code == 401


async def test_records_list_detail_and_media_flow(client, auth, records_api):
    listing = await client.get("/api/creative/records", headers=auth)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    item = body["items"][0]
    assert item["id"] == "lab-demo-run" and item["source"] == "lab"
    assert item["thumbnail_url"].startswith(
        "/api/creative/records/lab-demo-run/media/thumb?token="
    )
    assert (await client.get(item["thumbnail_url"])).status_code == 200
    filtered = await client.get("/api/creative/records?source=g0", headers=auth)
    assert filtered.json()["total"] == 0
    assert (
        await client.get("/api/creative/records?source=bogus", headers=auth)
    ).status_code == 422

    detail = await client.get("/api/creative/records/lab-demo-run", headers=auth)
    assert detail.status_code == 200, detail.text
    media = {m["id"]: m for m in detail.json()["media"]}
    clip = await client.get(media["clip"]["url"])
    assert clip.status_code == 200 and clip.headers["content-type"] == "video/mp4"
    assert clip.headers["x-content-type-options"] == "nosniff"
    ranged = await client.get(media["clip"]["url"], headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206 and ranged.content == b"0123"
    doc = await client.get(media["doc"]["url"])
    assert doc.headers["content-type"].startswith("text/plain")


async def test_record_media_token_is_scoped_to_record_and_media(
    client, auth, records_api
):
    detail = (
        await client.get("/api/creative/records/lab-demo-run", headers=auth)
    ).json()
    media = {m["id"]: m["url"] for m in detail["media"]}
    # 다른 미디어 id에 같은 토큰을 쓰면 거부
    assert (
        await client.get(media["clip"].replace("/media/clip", "/media/doc"))
    ).status_code == 403
    assert (
        await client.get(media["clip"].split("?")[0])
    ).status_code == 422  # 토큰 누락
    assert (
        await client.get(media["clip"].split("?")[0] + "?token=garbage")
    ).status_code == 403


async def test_record_api_rejects_traversal_and_unknown_ids(client, auth, records_api):
    for rid in ("..%2Fetc", "lab-..", "lab-Demo", "g0-123", "v1-xyz"):
        assert (
            await client.get(f"/api/creative/records/{rid}", headers=auth)
        ).status_code in (404, 422)
    assert (
        await client.get("/api/creative/records/lab-missing-run", headers=auth)
    ).status_code == 404
    detail = (
        await client.get("/api/creative/records/lab-demo-run", headers=auth)
    ).json()
    token = detail["media"][0]["url"].split("?token=")[1]
    for media_id in ("..", "%2e%2e", "a/b", "clip%2F..%2Fx"):
        res = await client.get(
            f"/api/creative/records/lab-demo-run/media/{media_id}?token={token}"
        )
        assert res.status_code in (401, 403, 404, 422)


async def test_record_api_never_serves_symlink_escape(
    client, auth, records_api, lab, tmp_path
):
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"top-secret")
    link = lab / "area/out/clip.mp4"
    link.unlink()
    os.symlink(secret, link)
    detail = (
        await client.get("/api/creative/records/lab-demo-run", headers=auth)
    ).json()
    clip = next(m for m in detail["media"] if m["id"] == "clip")
    assert clip["available"] is False and clip["url"] is None
    token = detail["media"][0]["url"].split("?token=")[1]
    forged = f"/api/creative/records/lab-demo-run/media/clip?token={token}"
    assert (await client.get(forged)).status_code == 403


async def test_g0_record_media_is_served_from_cas(client, auth, records_api):
    store, _ = records_api
    pid = store.create_production("미디어 있는 제작")["manifest"]["production_id"]
    store.put_artifact(pid, "clip.mp4", b"cas-bytes", media_type="video/mp4")
    detail = (await client.get(f"/api/creative/records/g0-{pid}", headers=auth)).json()
    url = detail["media"][0]["url"]
    res = await client.get(url)
    assert res.status_code == 200 and res.content == b"cas-bytes"
    listing = (await client.get("/api/creative/records?source=g0", headers=auth)).json()
    assert listing["total"] == 1 and listing["counts"]["g0"] == 1


def test_default_lab_root_setting_points_at_studio_lab():
    from ttakkari.config import Settings

    assert Settings(_env_file=None).studio_lab_root.name == "ttakkari-studio-lab"


def test_record_ids_are_unique_per_source(lab):
    folder = lab / "other" / "records" / "demo-run"
    folder.mkdir(parents=True)
    (folder / "record.json").write_text(json.dumps(manifest()))
    records, skipped = LabSource(lab).load_all()
    assert len(records) == 1 and any("중복" in s["reason"] for s in skipped)
    assert isinstance(Path(lab), Path)
