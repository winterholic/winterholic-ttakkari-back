import io

from PIL import Image


async def test_creative_user_flow_and_media_scope(client, auth):
    base = "/api/creative/projects"
    assert (await client.get(base)).status_code == 401
    r = await client.post(base, json={"name": "새벽 여행", "duration": 6}, headers=auth)
    assert r.status_code == 201, r.text
    p = r.json()
    pid = p["id"]
    root = base + "/" + pid
    assert len(p["scenes"]) == 3
    bad = await client.post(root + "/renders", json={}, headers=auth)
    assert bad.status_code == 422
    image = io.BytesIO()
    Image.new("RGB", (80, 80), "navy").save(image, format="PNG")
    r = await client.post(
        root + "/assets",
        headers=auth,
        files={"file": ("별.png", image.getvalue(), "image/png")},
    )
    assert r.status_code == 201, r.text
    asset = r.json()
    link = await client.post(
        root + "/media-link", headers=auth, json={"kind": "asset", "id": asset["id"]}
    )
    assert link.status_code == 200, link.text
    assert (await client.get(link.json()["url"])).status_code == 200
    ranged = await client.get(link.json()["url"], headers={"Range": "bytes=0-9"})
    assert ranged.status_code == 206 and len(ranged.content) == 10
    attachment = await client.get(link.json()["url"] + "&download=true")
    assert attachment.headers["content-disposition"].startswith("attachment;")
    other = (
        await client.post(base, json={"name": "다른 프로젝트"}, headers=auth)
    ).json()
    assert (
        await client.get(link.json()["url"].replace(pid, other["id"]))
    ).status_code == 403
    assert (
        await client.post(
            root + "/assets",
            headers=auth,
            files={"file": ("bad.svg", b"<svg/>", "image/svg+xml")},
        )
    ).status_code == 422
    latest = (await client.get(root, headers=auth)).json()
    scenes = [
        {**s, "asset_id": asset["id"], "caption": "별을 따라서"}
        for s in latest["scenes"]
    ]
    edited = await client.patch(
        root, headers=auth, json={"revision": latest["revision"], "scenes": scenes}
    )
    assert edited.status_code == 200, edited.text
    stale = await client.patch(
        root, headers=auth, json={"revision": latest["revision"], "scenes": scenes}
    )
    assert stale.status_code == 409


async def test_cpu_director_api_uses_existing_media_and_persists_attempt(
    client, auth, tmp_path, monkeypatch
):
    from ttakkari.api import creative as creative_api
    from ttakkari.creative import director

    monkeypatch.setattr(
        creative_api, "default_director_store", lambda: director.DirectorStore(tmp_path)
    )
    created = await client.post(
        "/api/creative/productions", headers=auth, json={"brief": "existing clip smoke"}
    )
    assert created.status_code == 201, created.text
    pid = created.json()["manifest"]["production_id"]
    uploaded = await client.post(
        f"/api/creative/productions/{pid}/artifacts",
        headers=auth,
        files={"file": ("existing.mp4", b"existing-media", "video/mp4")},
    )
    assert uploaded.status_code == 201, uploaded.text
    submitted = await client.post(
        f"/api/creative/productions/{pid}/attempts",
        headers=auth,
        json={
            "shot_id": "shot-1",
            "input_sha256": uploaded.json()["sha256"],
            "idempotency_key": "api-smoke",
        },
    )
    assert submitted.status_code == 202, submitted.text
    assert submitted.json()["state"] == "review_pending"
    persisted = await client.get(f"/api/creative/productions/{pid}", headers=auth)
    assert (
        persisted.json()["attempts"][0]["outputs"][0]["sha256"]
        == uploaded.json()["sha256"]
    )
