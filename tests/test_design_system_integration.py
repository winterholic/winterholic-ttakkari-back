"""Real app JWT + persisted personal/work sessions; independent test database."""
import json
import uuid

from conftest import git

from ttakkari.config import get_settings
from ttakkari.orchestrator.runtime import issue_token


async def test_owner_and_scoped_agent_read_same_committed_source(client, auth, make_git_dir, monkeypatch):
    root = make_git_dir()
    path = root / "ttakkari/tokens/src"
    path.mkdir(parents=True)
    (root / "ttakkari/package.json").write_text('{"name":"ttakkari","version":"1.0.0"}')
    (path / "color.light.json").write_text(json.dumps({"color": {"$type": "color", "blue": {"$value": "#123456"}}}))
    example = root / "ttakkari/examples/preview.html"
    example.parent.mkdir(parents=True)
    example.write_text('<html><head><title>미리보기</title></head><body><button>확인</button></body></html>')
    git(root, "add", ".")
    git(root, "commit", "-qm", "test system")
    monkeypatch.setattr(get_settings(), "design_system_root", root)
    assert (await client.get('/api/design-systems')).status_code == 401
    catalog = await client.get('/api/design-systems', headers=auth)
    assert catalog.status_code == 200 and catalog.json()['items'][0]['counts']['token'] == 1
    assert (await client.get('/api/design-systems/ttakkari/preview')).status_code == 401
    preview = await client.get('/api/design-systems/ttakkari/preview', headers=auth)
    assert preview.status_code == 200 and '<button>확인</button>' in preview.json()['html']
    assert preview.json()['revision'] == catalog.json()['revision']
    ws = (await client.post('/api/workspaces', json={'name': 'design systems', 'root_path': str(root)}, headers=auth)).json()
    for pool, code in [('personal', 200), ('work', 200)]:
        r = await client.post('/api/sessions', json={'workspace_id': ws['id'], 'pool': pool}, headers=auth)
        assert r.status_code == 201, r.text
        session = r.json()
        agent = {'Authorization': 'Bearer ' + issue_token(uuid.UUID(session['id']))}
        r = await client.get('/api/agent/design-systems/ttakkari/item', headers=agent,
                             params={'item': 'token:color.blue', 'revision': catalog.json()['revision']})
        assert r.status_code == code, r.text
        if pool == 'personal':
            assert r.json()['light']['resolved'] == '#123456'
    # API tokens and session tokens cannot be interchanged.
    assert (await client.get('/api/agent/design-systems', headers=auth)).status_code == 401
    assert (await client.get('/api/design-systems', headers=agent)).status_code == 401

    assert (await client.get('/api/design-systems/ttakkari/preview', headers=agent)).status_code == 401
