"""기존 게임 연결은 읽기 전용 진단이며 빌드·스크립트를 실행하지 않는다."""
import json

from test_api_games import make_repo, register

from ttakkari.games.contract import game_dir


async def test_discovery_requires_user_auth(client):
    assert (await client.get('/api/games/discover')).status_code == 401


async def test_discovery_finds_existing_game_without_changing_its_files(client, auth):
    root = make_repo('discover-ready')
    (root / 'package.json').write_text(json.dumps({'scripts': {'build': 'node do-not-run.js'}}))
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    r = await client.get('/api/games/discover', headers=auth)
    assert r.status_code == 200, r.text
    candidate = next(c for c in r.json()['candidates'] if c['slug'] == root.name)
    assert candidate['can_register'] is True
    assert candidate['game_id'] is None
    assert candidate['engine_id'] == 'web-phaser'
    assert any('플레이' in issue for issue in candidate['issues'])
    assert before == {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


async def test_discovery_marks_missing_invalid_and_linked_projects(client, auth, tmp_path):
    missing = game_dir('discover-missing')
    missing.mkdir(exist_ok=True)
    invalid = make_repo('discover-invalid')
    (invalid / 'ttakkari-game.json').write_text('{}')
    linked = game_dir('discover-linked')
    linked.symlink_to(tmp_path, target_is_directory=True)
    try:
        r = await client.get('/api/games/discover', headers=auth)
        assert r.status_code == 200, r.text
        candidates = {c['slug']: c for c in r.json()['candidates']}
        for slug in (missing.name, invalid.name, linked.name):
            assert not candidates[slug]['can_register']
            assert candidates[slug]['issues']
    finally:
        linked.unlink()


async def test_discovery_opens_registered_game_and_reports_truncation(client, auth):
    root = make_repo('discover-registered')
    make_repo('discover-second')
    g = (await register(client, auth, root.name)).json()
    r = await client.get('/api/games/discover', headers=auth)
    assert r.status_code == 200, r.text
    found = next(c for c in r.json()['candidates'] if c['slug'] == root.name)
    assert found['game_id'] == g['id'] and not found['can_register']
    limited = await client.get('/api/games/discover?limit=1', headers=auth)
    assert limited.status_code == 200
    assert len(limited.json()['candidates']) == 1
    assert limited.json()['has_more'] is True
