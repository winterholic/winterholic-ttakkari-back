"""Personal-only maintenance API; the domain's real DB/drain checks live in test_collection_scope_merge."""
import pytest
from test_host_operations import _hdr, _start, env, isolated_state  # noqa: F401

pytestmark = [pytest.mark.security]


@pytest.mark.parametrize("kind,applied", [("collection_scope_preview", False), ("collection_scope_apply", True)])
async def test_collection_scope_operation_is_personal_and_uses_the_guarded_domain(client, env, monkeypatch, kind, applied):  # noqa: F811
    from ttakkari.collector import scope_merge

    calls = []

    async def operate(db, *, apply):
        calls.append(apply)
        return {"applied": apply, "tables": {}, "unresolved": []}

    monkeypatch.setattr(scope_merge, "operate", operate)
    # ba4f0220: 소유자 작업은 회사 세션에도 열려 있어 pool 로 거부하지 않는다.
    work = await _start(client, _hdr(env.work_session_id), kind)
    assert work.status_code == 202, work.text
    calls.clear()
    result = await _start(client, _hdr(env.personal_session_id), kind)
    assert result.status_code == 202, result.text
    body = result.json()
    assert body["status"] == "succeeded" and body["summary"]["applied"] is applied
    assert calls == [applied] and body["audit_pending"] is False
