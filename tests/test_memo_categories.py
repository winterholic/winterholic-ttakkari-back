"""메모 카테고리·다중 태그 필터(ADR 0008)."""
from __future__ import annotations

from test_context import _agent, _session


async def _cat(client, auth, name, **kw) -> dict:
    r = await client.post("/api/memo-categories", json={"name": name, **kw}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


async def test_category_crud_order_and_delete_moves_memos_to_uncategorized(client, auth):
    work = await _cat(client, auth, "  회사  ", color="blue")
    home = await _cat(client, auth, "집")
    assert work["name"] == "회사" and home["position"] > work["position"]
    assert (await client.post("/api/memo-categories", json={"name": "회사"}, headers=auth)).status_code == 409
    assert (await client.post("/api/memo-categories", json={"name": "x", "color": "pink"}, headers=auth)).status_code == 422

    a = (await client.post("/api/memos", json={"title": "회의", "category_id": work["id"]}, headers=auth)).json()
    await client.post("/api/memos", json={"title": "장보기", "category_id": home["id"]}, headers=auth)
    await client.post("/api/memos", json={"title": "아무거나"}, headers=auth)
    assert a["category_id"] == work["id"]

    listing = (await client.get("/api/memo-categories", headers=auth)).json()
    assert [(c["name"], c["count"]) for c in listing["categories"]] == [("회사", 1), ("집", 1)]
    assert listing["total"] == 3 and listing["uncategorized"] == 1

    ordered = await client.put("/api/memo-categories/order", json={"ids": [home["id"], work["id"]]}, headers=auth)
    assert [c["name"] for c in ordered.json()["categories"]] == ["집", "회사"]
    renamed = await client.patch(f"/api/memo-categories/{work['id']}", json={"name": "업무", "color": None}, headers=auth)
    assert renamed.json()["name"] == "업무" and renamed.json()["color"] is None
    assert (await client.patch(f"/api/memo-categories/{work['id']}", json={"name": "집"}, headers=auth)).status_code == 409

    def titles(r):
        return [m["title"] for m in r.json()["memos"]]
    assert titles(await client.get("/api/memos", params={"category": work["id"]}, headers=auth)) == ["회의"]
    assert titles(await client.get("/api/memos", params={"category": "none"}, headers=auth)) == ["아무거나"]
    assert (await client.get("/api/memos", params={"category": "nope"}, headers=auth)).status_code == 422

    assert (await client.delete(f"/api/memo-categories/{work['id']}", headers=auth)).status_code == 204
    moved = (await client.get(f"/api/memos/{a['id']}", headers=auth)).json()
    assert moved["category_id"] is None and moved["rev"] == a["rev"] + 1
    assert sorted(titles(await client.get("/api/memos", params={"category": "none"}, headers=auth))) == ["아무거나", "회의"]


async def test_patch_category_keeps_or_clears_and_unknown_is_404(client, auth):
    c = await _cat(client, auth, "공부")
    m = (await client.post("/api/memos", json={"title": "t"}, headers=auth)).json()
    p = (await client.patch(f"/api/memos/{m['id']}", json={"category_id": c["id"]}, headers=auth)).json()
    assert p["category_id"] == c["id"]
    p = (await client.patch(f"/api/memos/{m['id']}", json={"title": "제목만"}, headers=auth)).json()
    assert p["category_id"] == c["id"]  # 보내지 않으면 그대로
    p = (await client.patch(f"/api/memos/{m['id']}", json={"category_id": None}, headers=auth)).json()
    assert p["category_id"] is None
    bad = await client.patch(f"/api/memos/{m['id']}", json={"category_id": "00000000-0000-0000-0000-000000000000"}, headers=auth)
    assert bad.status_code == 404


async def test_multiple_tags_narrow_and_tag_counts_follow_category(client, auth):
    c = await _cat(client, auth, "여행")
    await client.post("/api/memos", json={"title": "제주", "tags": ["여름", "바다"], "category_id": c["id"]}, headers=auth)
    await client.post("/api/memos", json={"title": "부산", "tags": ["바다"], "category_id": c["id"]}, headers=auth)
    await client.post("/api/memos", json={"title": "수박", "tags": ["여름"]}, headers=auth)
    both = (await client.get("/api/memos", params=[("tag", "여름"), ("tag", "바다")], headers=auth)).json()["memos"]
    assert [m["title"] for m in both] == ["제주"]
    scoped = (await client.get("/api/memos/tags", params={"category": c["id"]}, headers=auth)).json()["tags"]
    assert scoped == [{"name": "바다", "count": 2}, {"name": "여름", "count": 1}]
    loose = (await client.get("/api/memos/tags", params={"category": "none"}, headers=auth)).json()["tags"]
    assert loose == [{"name": "여름", "count": 1}]


async def test_agent_uses_category_names(client, auth, make_dir):
    h = _agent((await _session(client, auth, make_dir))["id"])
    await _cat(client, auth, "회사")
    assert (await client.get("/api/agent/memo-categories", headers=h)).json() == {"categories": [{"name": "회사", "color": None}]}
    m = (await client.post("/api/agent/memos", json={"body": "주간 보고", "tags": ["보고"], "category": "회사"}, headers=h)).json()
    assert m["category"] == "회사" and m["category_id"]
    missing = await client.post("/api/agent/memos", json={"body": "x", "category": "없는거"}, headers=h)
    assert missing.status_code == 422 and "회사" in missing.text
    found = (await client.get("/api/agent/memos", params={"category": "회사", "tag": "보고"}, headers=h)).json()["memos"]
    assert [x["id"] for x in found] == [m["id"]] and found[0]["category"] == "회사"
    user = (await client.post("/api/memos", json={"body": "내 메모"}, headers=auth)).json()
    moved = (await client.patch(f"/api/agent/memos/{user['id']}", json={"category": "회사"}, headers=h)).json()
    assert moved["category"] == "회사"
    cleared = (await client.patch(f"/api/agent/memos/{user['id']}", json={"category": ""}, headers=h)).json()
    assert cleared["category"] is None
    assert [x["id"] for x in (await client.get("/api/agent/memos", params={"category": "none"}, headers=h)).json()["memos"]] == [user["id"]]
