"""고정 도서관 목록 가져오기 host operation(context_catalog_preview/apply): 경로 고정, 롤백 미리보기, 색인 요청."""
from __future__ import annotations

import hashlib
import json
import typing

import pytest
from conftest import ALLOWED
from pydantic import ValidationError
from sqlalchemy import func, select
from test_host_operations import _hdr, _start, env, isolated_state  # noqa: F401

from ttakkari.api.assistant import wait_indexing
from ttakkari.api.operations import DESCRIPTIONS, StartIn
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import CtxEntity, CtxMemory, CtxSource
from ttakkari.operations import plans

pytestmark = [pytest.mark.security]
KINDS = ("context_catalog_preview", "context_catalog_apply")


def _catalog(doc_dir, *, summary="공유 연구 묶음", fact="처음 사실", extra_source=None) -> dict:
    sources = [{"path": str(doc_dir), "category": "projects"}]
    if extra_source:
        sources.append({"path": str(extra_source), "category": "knowledge"})
    return {"projects": [{"name": "Research Bundle", "aliases": ["연구묶음"], "scope": "work", "summary": summary,
                          "sources": sources,
                          "facts": [{"key": "purpose", "content": fact, "source_ref": "card"}]}],
            "relations": []}


@pytest.fixture
def fixed_catalog(monkeypatch):
    """설정에 고정된 목록 파일 한 개. 테스트는 이 경로만 바꾼다(에이전트 입력으로는 바꿀 수 없다)."""
    docs = ALLOWED / "bundle-docs"
    docs.mkdir(exist_ok=True)
    (docs / "a.md").write_text("# 연구 묶음\n고유표식비교 자료\n")
    path = ALLOWED / "catalog-fixed.json"

    def write(data) -> bytes:
        raw = json.dumps(data, ensure_ascii=False).encode()
        path.write_bytes(raw)
        return raw

    monkeypatch.setattr(get_settings(), "context_catalog_file", path)
    return docs, path, write


async def _counts() -> tuple[int, int, int]:
    async with sessionmaker()() as db:
        return tuple([await db.scalar(select(func.count()).select_from(m))
                      for m in (CtxEntity, CtxSource, CtxMemory)])


def test_kinds_are_registered_and_accept_only_kind():
    for kind in KINDS:
        assert kind in plans.INLINE and kind in plans.KINDS and kind not in plans.HEAVY
        assert kind in DESCRIPTIONS
        assert StartIn(kind=kind).kind == kind
        for extra in ({"path": "/etc/passwd"}, {"catalog": "x.json"}, {"db_url": "postgresql://x"}):
            with pytest.raises(ValidationError):
                StartIn.model_validate({"kind": kind, **extra})
    from ttakkari.orchestrator import mcp_server

    hints = typing.get_type_hints(mcp_server.host_operation_start.fn if hasattr(mcp_server.host_operation_start, "fn")
                                  else mcp_server.host_operation_start)
    assert set(KINDS) <= set(typing.get_args(hints["kind"]))


async def test_preview_reports_counts_and_changes_nothing(client, env, fixed_catalog):  # noqa: F811
    docs, _, write = fixed_catalog
    raw = write(_catalog(docs))
    before = await _counts()
    r = await _start(client, _hdr(env.personal_session_id), "context_catalog_preview")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "succeeded", body
    s = body["summary"]
    assert s["applied"] is False
    assert s["catalog"]["sha256"] == hashlib.sha256(raw).hexdigest() and s["catalog"]["name"] == "catalog-fixed.json"
    assert s["projects"]["new"] == 1 and s["projects"]["new_names"] == ["Research Bundle"]
    assert s["sources"]["new"] == 1 and s["facts"]["new"] == 1
    assert s["overwritten"] == {"projects": 0, "facts": 0, "source_links": 0}
    assert s["index"] == {"would_request": 1}
    assert await _counts() == before, "preview 는 DB 를 바꾸지 않는다"
    assert "고유표식비교" not in json.dumps(s, ensure_ascii=False) and "처음 사실" not in json.dumps(s, ensure_ascii=False)


async def test_apply_imports_and_requests_indexing_of_new_sources_only(client, auth, env, fixed_catalog):  # noqa: F811
    docs, _, write = fixed_catalog
    write(_catalog(docs))
    hdr = _hdr(env.personal_session_id)
    r = await _start(client, hdr, "context_catalog_apply")
    assert r.status_code == 202, r.text
    s = r.json()["summary"]
    assert r.json()["status"] == "succeeded" and s["applied"] is True
    assert s["projects"]["new"] == 1 and s["sources"]["new"] == 1
    assert s["index"] == {"requested": 1, "failed": 0}
    await wait_indexing()
    async with sessionmaker()() as db:
        src = (await db.scalars(select(CtxSource))).one()
        ent = await db.get(CtxEntity, src.entity_id)
        assert ent.name == "Research Bundle" and ent.scope == "work" and src.scope == "work"
        assert src.last_indexed_at is not None and src.last_error is None
    hits = (await client.post("/api/assistant/recall", json={"query": "고유표식비교"}, headers=auth)).json()["results"]
    assert any(h["type"] == "chunk" for h in hits), "새 출처가 색인돼 검색된다"

    # 같은 목록을 다시 적용하면 바뀐 것도, 색인 요청도 없다.
    again = (await _start(client, hdr, "context_catalog_apply")).json()["summary"]
    assert again["projects"]["unchanged"] == 1 and again["sources"]["unchanged"] == 1
    assert again["facts"]["unchanged"] == 1 and again["overwritten"] == {"projects": 0, "facts": 0, "source_links": 0}
    assert again["index"] == {"requested": 0, "failed": 0}


async def test_overwrites_of_existing_values_are_counted_in_preview_and_apply(client, env, fixed_catalog):  # noqa: F811
    docs, _, write = fixed_catalog
    hdr = _hdr(env.personal_session_id)
    write(_catalog(docs))
    await _start(client, hdr, "context_catalog_apply")
    await wait_indexing()
    write(_catalog(docs, summary="바뀐 요약", fact="바뀐 사실"))
    preview = (await _start(client, hdr, "context_catalog_preview")).json()["summary"]
    assert preview["overwritten"] == {"projects": 1, "facts": 1, "source_links": 0}
    assert preview["projects"]["changed_names"] == ["Research Bundle"] and preview["index"] == {"would_request": 0}
    async with sessionmaker()() as db:
        assert (await db.scalar(select(CtxEntity.summary).where(CtxEntity.name == "Research Bundle"))) == "공유 연구 묶음", "preview 는 기존 값을 덮어쓰지 않는다"
        assert (await db.scalar(select(CtxMemory.content).where(CtxMemory.source == "ingest", CtxMemory.source_ref.startswith("catalog:Research Bundle")))) == "처음 사실"
    applied = (await _start(client, hdr, "context_catalog_apply")).json()["summary"]
    assert applied["overwritten"] == {"projects": 1, "facts": 1, "source_links": 0}
    async with sessionmaker()() as db:
        assert (await db.scalar(select(CtxEntity.summary).where(CtxEntity.name == "Research Bundle"))) == "바뀐 요약"
        assert (await db.scalar(select(CtxMemory.content).where(CtxMemory.source == "ingest", CtxMemory.source_ref.startswith("catalog:Research Bundle")))) == "바뀐 사실"


async def test_preview_failure_rolls_back_and_reports_without_leaking(client, env, fixed_catalog):  # noqa: F811
    docs, _, write = fixed_catalog
    # 두 번째 출처 경로가 없어 검증에서 막힌다. 첫 프로젝트도 쓰이면 안 된다.
    write(_catalog(docs, extra_source=ALLOWED / "missing-dir"))
    before = await _counts()
    r = (await _start(client, _hdr(env.personal_session_id), "context_catalog_apply")).json()
    assert r["status"] == "failed" and "그런 폴더나 파일이 없어요" in r["error"]
    assert await _counts() == before


@pytest.mark.parametrize("make", ["sensitive", "not_json", "too_big", "missing"])
async def test_fixed_file_validation_is_reused(client, env, fixed_catalog, monkeypatch, make):  # noqa: F811
    docs, _, _ = fixed_catalog
    if make == "sensitive":
        target = ALLOWED / ".ssh" / "catalog.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(_catalog(docs)))
    elif make == "not_json":
        target = ALLOWED / "catalog.txt"
        target.write_text(json.dumps(_catalog(docs)))
    elif make == "too_big":
        target = ALLOWED / "big.json"
        target.write_text(json.dumps({"projects": [], "pad": "x" * (2 * 1024 * 1024)}))
    else:
        target = ALLOWED / "nope.json"
    monkeypatch.setattr(get_settings(), "context_catalog_file", target)
    before = await _counts()
    for kind in KINDS:
        r = (await _start(client, _hdr(env.personal_session_id), kind)).json()
        assert r["status"] == "failed", r
    assert await _counts() == before
