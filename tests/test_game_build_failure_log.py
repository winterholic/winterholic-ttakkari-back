"""Actual build service with a synthetic adapter failure; no container or browser."""
from test_game_build_snapshots import FakeAdapter, make_game

from ttakkari.db import sessionmaker
from ttakkari.games.adapter import BuildResult
from ttakkari.games.builds import build_game
from ttakkari.models import GameProject


async def test_failed_build_emits_original_bounded_log(client, tmp_path, caplog):
    game_id, _ = await make_game('failure-original-log')
    marker = '/synthetic/mount-source/path/build-id'

    class FailedAdapter(FakeAdapter):
        async def build(self, project_path, build_id):
            return BuildResult(False, build_id, log='x' * 700 + marker + 'y' * 700,
                               error='synthetic install exit 127')

    async with sessionmaker()() as db:
        game = await db.get(GameProject, game_id)
        build = await build_game(db, game, adapter=FailedAdapter(tmp_path / 'work'))
        assert build.status == 'failed'
        assert marker in build.log_tail and marker in caplog.text
        assert str(build.id) in caplog.text
