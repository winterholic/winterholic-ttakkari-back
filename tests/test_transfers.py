"""폰 → 컴퓨터 파일 보내기: 조각 업로드·이어 올리기, 아이폰 사진 손질, 목적지 범위, 단축어 토큰."""
from __future__ import annotations

import io
from pathlib import Path

import pillow_heif
import pytest
from conftest import ALLOWED, HOME
from PIL import Image

pillow_heif.register_heif_opener()

DOCS = ALLOWED / "default-docs"


def _photo(fmt: str, *, gps: bool = True, orientation: int | None = None) -> bytes:
    im = Image.new("RGB", (40, 20), "red")
    ex = Image.Exif()
    if orientation:
        ex[0x0112] = orientation
    ex.get_ifd(0x8769)[0x9003] = "2026:10:11 15:30:12"
    if gps:
        ex.get_ifd(0x8825)[1] = "N"
        ex.get_ifd(0x8825)[2] = (37.0, 30.0, 0.0)
    b = io.BytesIO()
    im.save(b, format=fmt, exif=ex.tobytes())
    return b.getvalue()


async def _send(client, auth, name: str, data: bytes, dest: Path, chunk: int = 1024, **opts) -> dict:
    r = await client.post("/api/transfers", json={"filename": name, "size": len(data), "dest_dir": str(dest), **opts},
                          headers=auth)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    for off in range(0, len(data), chunk):
        r = await client.put(f"/api/transfers/{tid}", params={"offset": off}, content=data[off:off + chunk], headers=auth)
        assert r.status_code == 200, r.text
    r = await client.post(f"/api/transfers/{tid}/complete", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


async def test_chunked_upload_resumes_after_gap_and_lands_in_folder(client, auth):
    data = bytes(range(256)) * 20  # 5120 bytes
    dest = DOCS / "inbox"
    r = await client.post("/api/transfers", json={"filename": "보고서.pdf", "size": len(data), "dest_dir": str(dest)}, headers=auth)
    tid = r.json()["id"]
    assert r.json()["chunk_size"] == 1024
    await client.put(f"/api/transfers/{tid}", params={"offset": 0}, content=data[:1024], headers=auth)
    # 끊겼다 다시 붙어 엉뚱한 위치를 보내면 서버가 가진 크기를 알려 준다.
    r = await client.put(f"/api/transfers/{tid}", params={"offset": 3000}, content=data[3000:4000], headers=auth)
    assert r.status_code == 409 and r.json()["message"] == "received=1024"
    assert (await client.get(f"/api/transfers/{tid}", headers=auth)).json()["received"] == 1024
    r = await client.post(f"/api/transfers/{tid}/complete", headers=auth)
    assert r.status_code == 409  # 아직 덜 받음
    for off in range(1024, len(data), 1024):
        await client.put(f"/api/transfers/{tid}", params={"offset": off}, content=data[off:off + 1024], headers=auth)
    done = (await client.post(f"/api/transfers/{tid}/complete", headers=auth)).json()
    f = done["files"][0]
    assert f["name"] == "보고서.pdf" and Path(f["path"]).read_bytes() == data and f["artifact_id"]
    art = (await client.get(f"/api/artifacts/{f['artifact_id']}", headers=auth)).json()
    assert art["source"] == "upload"


async def test_heic_from_iphone_becomes_dated_jpg_without_location(client, auth):
    done = await _send(client, auth, "image.heic", _photo("HEIF"), DOCS)
    f = done["files"][0]
    assert f["name"] == "IMG_20261011_153012.jpg"
    assert "HEIC 사진을 JPG로 바꿨어요" in done["notes"] and "위치 정보를 지웠어요" in done["notes"]
    with Image.open(f["path"]) as im:
        assert im.format == "JPEG"
        assert not dict(im.getexif().get_ifd(0x8825))
        assert im.getexif().get_ifd(0x8769).get(0x9003) == "2026:10:11 15:30:12"


async def test_heic_keep_original_and_no_conversion(client, auth):
    done = await _send(client, auth, "image.HEIC", _photo("HEIF"), DOCS / "keep", keep_original=True)
    assert sorted(Path(f["name"]).suffix for f in done["files"]) == [".heic", ".jpg"]
    raw = await _send(client, auth, "IMG_0001.heic", _photo("HEIF"), DOCS / "raw", convert_heic=False)
    assert raw["files"][0]["name"] == "IMG_0001.heic"


async def test_jpeg_location_removed_but_orientation_kept(client, auth):
    done = await _send(client, auth, "image.jpg", _photo("JPEG", orientation=6), DOCS / "jpg")
    with Image.open(done["files"][0]["path"]) as im:
        ex = im.getexif()
        assert not dict(ex.get_ifd(0x8825))
        # 픽셀을 돌리지 않았으니 방향 태그가 남아 있어야 뷰어가 바로 세운다.
        assert ex.get(0x0112) == 6


async def test_location_kept_when_asked_and_named_file_kept(client, auth):
    done = await _send(client, auth, "여행.jpg", _photo("JPEG"), DOCS / "trip", strip_location=False)
    f = done["files"][0]
    assert f["name"] == "여행.jpg"
    with Image.open(f["path"]) as im:
        assert dict(im.getexif().get_ifd(0x8825))


async def test_same_name_gets_numbered(client, auth):
    a = await _send(client, auth, "memo.txt", b"one", DOCS / "dup")
    b = await _send(client, auth, "memo.txt", b"two", DOCS / "dup")
    assert a["files"][0]["name"] == "memo.txt" and b["files"][0]["name"] == "memo (2).txt"


async def test_generic_video_name_is_dated(client, auth):
    done = await _send(client, auth, "video.mov", b"\x00\x00\x00\x14ftypqt  ", DOCS / "vid")
    assert done["files"][0]["name"].startswith("VID_") and done["files"][0]["name"].endswith(".mov")


async def test_destination_must_be_inside_permission_folders(client, auth, outside_dir):
    for bad in (str(outside_dir), str(HOME / "data"), str(HOME / "config.env.d"), "relative/dir"):
        r = await client.post("/api/transfers", json={"filename": "a.txt", "size": 1, "dest_dir": bad}, headers=auth)
        assert r.status_code in (400, 403), (bad, r.text)


async def test_too_big_is_rejected_up_front(client, auth):
    r = await client.post("/api/transfers", json={"filename": "a.mov", "size": 10**15, "dest_dir": str(DOCS)}, headers=auth)
    assert r.status_code == 413


async def test_folders_list_roots_and_children_and_mkdir(client, auth):
    top = (await client.get("/api/transfers/folders", headers=auth)).json()
    assert str(DOCS.resolve()) in [r["path"] for r in top["roots"]] and top["default"] == str((HOME / "inbox").resolve())
    r = await client.post("/api/transfers/folders", json={"path": str(DOCS / "사진")}, headers=auth)
    assert r.status_code == 201
    sub = (await client.get("/api/transfers/folders", params={"path": str(DOCS)}, headers=auth)).json()
    assert "사진" in [d["name"] for d in sub["dirs"]] and sub["parent"] is None and sub["can_write"]


async def test_shortcut_token_uploads_and_can_be_revoked(client, auth):
    t = (await client.post("/api/transfer-tokens", json={"name": "아이폰"}, headers=auth)).json()
    assert t["token"].startswith("ttk_up_")
    h = {"Authorization": f"Bearer {t['token']}"}
    r = await client.post("/api/transfers/simple", files=[("files", ("image.heic", _photo("HEIF"), "image/heic")),
                                                          ("files", ("memo.txt", b"hi", "text/plain"))], headers=h)
    assert r.status_code == 201, r.text
    names = sorted(f["name"] for f in r.json()["files"])
    assert names == ["IMG_20261011_153012.jpg", "memo.txt"]
    # 기본 받는 폴더는 ~/.ttakkari/inbox
    assert all(Path(f["path"]).parent == (HOME / "inbox").resolve() for f in r.json()["files"])
    listed = (await client.get("/api/transfer-tokens", headers=auth)).json()
    assert listed[0]["last_used_at"] and listed[0]["token"] is None
    # 업로드 토큰으로는 다른 API 를 못 쓴다.
    assert (await client.get("/api/sessions", headers=h)).status_code == 401
    await client.delete(f"/api/transfer-tokens/{t['id']}", headers=auth)
    r = await client.post("/api/transfers/simple", files=[("files", ("a.txt", b"x", "text/plain"))], headers=h)
    assert r.status_code == 401


async def test_simple_upload_with_user_token_and_dest(client, auth):
    r = await client.post("/api/transfers/simple", files=[("files", ("a.txt", b"x", "text/plain"))],
                          data={"dest_dir": str(DOCS / "here")}, headers=auth)
    assert r.status_code == 201 and Path(r.json()["files"][0]["path"]).parent == (DOCS / "here").resolve()
    assert (await client.post("/api/transfers/simple", files=[("files", ("a.txt", b"x", "text/plain"))])).status_code == 401


async def test_cors_preflight_allows_chunk_put(client):
    # 조각 업로드는 PUT + application/octet-stream 이라 브라우저가 preflight 를 보낸다(배포에서만 드러난 버그).
    r = await client.options("/api/transfers/x?offset=0", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "authorization,content-type"})
    assert r.status_code == 200 and "PUT" in r.headers["access-control-allow-methods"]


async def test_shortcut_raw_upload_preserves_binary_and_encoded_filename(client, auth):
    # 단축어의 요청 본문=파일: 파일 바이트 자체와 URL 인코딩된 이름을 보낸다.
    name = "보고서 & 메모 + #1 📎.pdf"
    data = b"%PDF-1.7\n" + bytes(range(256))
    token = (await client.post("/api/transfer-tokens", json={}, headers=auth)).json()
    h = {"Authorization": f"Bearer {token['token']}", "Content-Type": "application/octet-stream"}
    r = await client.post("/api/transfers/raw", params={"filename": name}, content=data, headers=h)
    assert r.status_code == 201, r.text
    f = r.json()["files"][0]
    assert f["name"] == name and f["size"] == len(data) and f["artifact_id"]
    assert Path(f["path"]).parent == (HOME / "inbox").resolve()
    assert Path(f["path"]).read_bytes() == data
    assert (await client.get("/api/sessions", headers=h)).status_code == 401
    listed = (await client.get("/api/transfer-tokens", headers=auth)).json()
    assert listed[0]["last_used_at"]
    await client.delete(f"/api/transfer-tokens/{token['id']}", headers=auth)
    assert (await client.post("/api/transfers/raw", params={"filename": name}, content=data, headers=h)).status_code == 401


async def test_shortcut_raw_heic_uses_existing_photo_processing(client, auth):
    r = await client.post("/api/transfers/raw", params={"filename": "image.heic", "dest_dir": str(DOCS / "raw-photo")},
                          content=_photo("HEIF"), headers={**auth, "Content-Type": "application/octet-stream"})
    assert r.status_code == 201, r.text
    f = r.json()["files"][0]
    assert f["name"] == "IMG_20261011_153012.jpg"
    with Image.open(f["path"]) as im:
        assert im.format == "JPEG" and not dict(im.getexif().get_ifd(0x8825))


async def test_shortcut_raw_can_keep_original_heic_and_choose_destination(client, auth):
    dest = DOCS / "raw-shortcut"
    data = _photo("HEIF")
    r = await client.post("/api/transfers/raw", params={"filename": "IMG_0001.heic", "dest_dir": str(dest),
                          "convert_heic": "false", "strip_location": "false"},
                          content=data, headers=auth)
    assert r.status_code == 201, r.text
    f = r.json()["files"][0]
    assert Path(f["path"]).parent == dest.resolve() and Path(f["path"]).read_bytes() == data


async def test_shortcut_raw_empty_file_and_repeated_input(client, auth):
    for expected in ("empty.txt", "empty (2).txt"):
        r = await client.post("/api/transfers/raw", params={"filename": "empty.txt"}, content=b"", headers=auth)
        assert r.status_code == 201, r.text
        assert r.json()["files"][0]["name"] == expected
        assert Path(r.json()["files"][0]["path"]).read_bytes() == b""


async def test_shortcut_raw_requires_auth_name_and_allowed_destination(client, auth, outside_dir):
    assert (await client.post("/api/transfers/raw", params={"filename": "x.bin"}, content=b"x")).status_code == 401
    for params in ({}, {"filename": ""}):
        assert (await client.post("/api/transfers/raw", params=params, content=b"x", headers=auth)).status_code == 422
    r = await client.post("/api/transfers/raw", params={"filename": "x.bin", "dest_dir": str(outside_dir)},
                          content=b"x", headers=auth)
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("mime", ["application/json", "multipart/form-data; boundary=oops", "application/x-www-form-urlencoded"])
async def test_shortcut_raw_rejects_wrong_request_body_mode(client, auth, mime):
    r = await client.post("/api/transfers/raw", params={"filename": "x.bin"}, content=b'{"files":"wrong"}',
                          headers={**auth, "Content-Type": mime})
    assert r.status_code == 415 and r.json()["code"] == "wrong_body_type"


async def test_shortcut_raw_stream_limit_and_disconnect_clean_staging(client, auth, monkeypatch):
    from ttakkari.config import get_settings
    from ttakkari.transfer import service
    monkeypatch.setattr(get_settings(), "transfer_max_bytes", 4)
    before = set(service.staging_root().iterdir()) if service.staging_root().exists() else set()

    async def chunks():
        yield b"123"
        yield b"45"

    r = await client.post("/api/transfers/raw", params={"filename": "large.bin"}, content=chunks(), headers=auth)
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    r = await client.post("/api/transfers/raw", params={"filename": "large.bin"}, content=b"12345", headers=auth)
    assert r.status_code == 413

    async def broken():
        yield b"12"
        raise RuntimeError("upload interrupted")

    with pytest.raises(RuntimeError, match="upload interrupted"):
        await client.post("/api/transfers/raw", params={"filename": "broken.bin"}, content=broken(), headers=auth)
    assert set(service.staging_root().iterdir()) == before
