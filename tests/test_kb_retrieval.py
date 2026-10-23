import pytest


@pytest.fixture(autouse=True)
def enable_semantic():
    from ttakkari.config import get_settings
    s = get_settings()
    old = s.kb_semantic_enabled
    s.kb_semantic_enabled = True
    yield
    s.kb_semantic_enabled = old


async def register(client, auth, text, scope="personal"):
    r = await client.post("/api/assistant/kb/documents", headers=auth,
                          json={"title": text[:30], "text": text, "scope": scope})
    assert r.status_code == 201
    return r.json()


async def test_search_mode_is_validated(client, auth):
    r = await client.get("/api/assistant/kb/search?q=test&mode=invalid", headers=auth)
    assert r.status_code == 422


async def test_hybrid_projection_is_idempotent_and_scope_filtered(client, auth):
    one = await register(client, auth, "트랜잭션 격리 수준과 직렬화 충돌을 설명한다.")
    two = await register(client, auth, "기상청은 남부 지역에 많은 비가 내릴 것으로 예측했다.")
    private = await register(client, auth, "트랜잭션 격리 회사전용기록", scope="work")
    from ttakkari import kb_retrieval
    assert await kb_retrieval.index_pending(limit=30) == 3
    assert await kb_retrieval.index_pending(limit=30) == 0
    hits = await client.get("/api/assistant/kb/search?q=트랜잭션&mode=hybrid", headers=auth)
    assert hits.status_code == 200, hits.text
    body = hits.json()
    assert body["retrieval"]["quality_gate"] == "unverified"
    assert body["retrieval"]["model"] == "hash"
    assert body["items"][0]["document"]["id"] == one["document_id"]
    assert private["document_id"] not in {item["document"]["id"] for item in body["items"]}
    assert two["document_id"] != one["document_id"]


async def test_failed_dense_query_is_reported_not_silently_relabelled(client, auth, monkeypatch):
    from ttakkari.context import embedder
    async def fail(*args, **kwargs):
        raise RuntimeError("deliberate model failure")
    monkeypatch.setattr(embedder, "embed", fail)
    r = await client.get("/api/assistant/kb/search?q=hello&mode=hybrid", headers=auth)
    assert r.status_code == 503


async def test_oversize_passage_does_not_starve_following_documents(client, auth):
    await register(client, auth, "x" * 50001)
    await register(client, auth, "searchable ordinary passage")
    from ttakkari import kb_retrieval
    assert await kb_retrieval.index_pending(limit=30) == 1
    assert await kb_retrieval.index_pending(limit=30) == 0
    summary = (await client.get("/api/assistant/kb/summary", headers=auth)).json()
    assert summary["retrieval"]["indexed"] == 1
    assert summary["retrieval"]["failed"] == 1


@pytest.mark.parametrize("text", ["a" * 2000, "한글 시험입니다." * 300, "line\n" * 800])
def test_embedding_windows_cover_the_whole_passage(text):
    from ttakkari.kb_retrieval import windows
    chunks = windows(text)
    reconstructed = chunks[0]
    for chunk in chunks[1:]:
        reconstructed += chunk[64:]
    assert reconstructed == text
    assert max(map(len, chunks)) <= 384

pytestmark = pytest.mark.security
