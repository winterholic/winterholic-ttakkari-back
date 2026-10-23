import pytest

"""Full managed-source -> local HTTP -> source policy -> immutable KB -> scoped search journey."""
from collector_support import article_html, prose
from test_collector_control import BASE, source_body
from test_collector_pipeline import (
    env,  # noqa: F401 -- import the isolated HTTP/PG fixture
)

from ttakkari import kb_delivery
from ttakkari.collector import runner


async def test_collection_reaches_search_with_original_bytes(client, auth, env, monkeypatch):  # noqa: F811 -- pytest fixture injection
    html = article_html("PostgreSQL 격리", [prose("ko", 33, 12) + " 직렬화 트랜잭션 격리 수준을 설명한다."])
    env.server.page("/document", html)
    body = source_body(seeds=[env.server.url("/document")], max_pages=1)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    source = await client.post(BASE + "/sources", headers=auth, json=body)
    assert source.status_code == 201, source.text
    job = await client.post(BASE + f"/sources/{source.json()['id']}/run", headers=auth)
    assert job.status_code == 202, job.text
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    assert await kb_delivery.deliver_once() == 1
    hits = (await client.get("/api/assistant/kb/search?q=직렬화", headers=auth)).json()
    assert len(hits["items"]) == 1
    hit = hits["items"][0]
    assert hit["source"]["byte_size"] == len(html.encode())
    assert hit["source"]["kind"] == "web"
    ev = (await client.get("/api/assistant/kb/passages/" + hit["passage_id"] + "/evidence", headers=auth)).json()
    assert ev["span"]["hash_verified"]
    assert "직렬화 트랜잭션" in ev["quote"]
    hidden = (await client.get("/api/assistant/kb/search?q=직렬화&scope=work", headers=auth)).json()
    assert hidden["items"] == []
    items = (await client.get(BASE + f"/jobs/{job.json()['job_id']}/items", headers=auth)).json()
    item = items["items"][0]
    assert item["delivery"]["outcome"] == "imported"
    review = await client.post(BASE + f"/items/{item['id']}/reviews", headers=auth,
                               json={"decision": "accept", "reason": "원문을 보고 유용한 참고 자료로 판단"})
    assert review.status_code == 201
    assert review.json()["factuality"] == "unverified"
    training = await client.post(BASE + "/quality/train", headers=auth)
    assert training.status_code == 422  # insufficient labels and no train grant; never invent a model
    assert (await client.get(BASE + "/quality", headers=auth)).json()["model"] is None
    revoked = dict(body["policy"])
    revoked["permissions"] = {"store": "allowed", "knowledge": "denied", "export": "denied"}
    changed = await client.patch(BASE + f"/sources/{source.json()['id']}", headers=auth,
                                 json={"policy": revoked, "expected_version": source.json()["version"]})
    assert changed.status_code == 200, changed.text
    assert (await client.get("/api/assistant/kb/search?q=직렬화", headers=auth)).json()["items"] == []
    assert (await client.get(BASE + f"/items/{item['id']}/export", headers=auth)).status_code == 403

pytestmark = pytest.mark.security
