"""Run the recorded Game Studio API build, without launching a browser."""
import pytest


class BuildOnly:
    def pytest_collection_modifyitems(self, items):
        for item in items:
            if item.name == 'test_game_studio_flow_with_recorded_worker_output':
                item.obj = self.first_build

    async def first_build(self, client, auth, monkeypatch, tmp_path):
        import os

        from helpers import fresh_dir
        from sqlalchemy import text
        from sqlalchemy.engine import make_url
        from test_api_games import register, wait_build
        from test_e2e_game_studio import DECISIONS, apply_fixture

        from ttakkari.db import sessionmaker
        from ttakkari.games.adapter import PhaserAdapter
        from ttakkari.games.contract import ProjectManifest, game_dir
        expected = make_url(os.environ['TTAKKARI_TEST_DB'])
        assert expected.database != 'ttakkari'
        async with sessionmaker()() as db:
            actual = (await db.execute(text('SELECT current_database(), current_user'))).one()
            assert actual[0] == expected.database
            assert not expected.username or actual[1] == expected.username
        slug = 'dot-dungeon-install-probe'
        root = fresh_dir(game_dir(slug))
        await PhaserAdapter().scaffold_project(root, ProjectManifest(
            id=slug, name='install probe', genre='로그라이크', engine='web-phaser',
            version='0.1.0', entrypoint='index.html', locked_decisions=DECISIONS))
        created = await register(client, auth, slug)
        assert created.status_code == 201, created.text
        gid = created.json()['id']
        approved = await client.post(f'/api/games/{gid}/lock/approve', headers=auth)
        assert approved.status_code == 200, approved.text
        apply_fixture(root, 'v1', 'Recorded source install diagnostic')
        started = await client.post(f'/api/games/{gid}/builds', json={}, headers=auth)
        assert started.status_code == 202, started.text
        done = await wait_build(client, auth, gid, started.json()['id'], timeout=600)
        print('BUILD_DIAGNOSTIC', done['status'], done['error'])
        print(done.get('log_tail', ''))
        assert done['status'] == 'succeeded', f"{done['error']}\n{done.get('log_tail', '')}"
        assert done['lock_verified'] is True

if __name__ == '__main__':
    raise SystemExit(pytest.main([
        'tests/test_e2e_game_studio.py::test_game_studio_flow_with_recorded_worker_output',
        '-q', '-s', '--timeout=650', '--tb=short'], plugins=[BuildOnly()]))
