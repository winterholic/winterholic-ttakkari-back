"""실제 게임 요청 경로(API → worker 세션 → Run prompt)로 스튜디오 키트가 전달되는지 (DB 필요)."""

import asyncio
import subprocess

from helpers import wait_run
from test_api_games import fake_adapter  # noqa: F401
from test_game_build_snapshots import _cleanup_snapshots  # noqa: F401
from test_game_worker_flow import ask, new_game

from ttakkari.games import studio
from ttakkari.games.contract import game_dir
from ttakkari.games.studio import ENTRY, SKILL_NAME


async def test_real_request_path_installs_kit_and_prompt_points_to_it(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "wk-studio", build=True)
    req = (await ask(client, auth, game["id"], kind="modify", instruction="1장에서 이동이 안 돼서 진행을 못 하겠어")).json()
    prompt = (await client.get(f"/api/runs/{req['run_id']}", headers=auth)).json()["prompt"]
    assert "[스튜디오 제작 계약]" in prompt and ENTRY in prompt
    assert prompt.index("[스튜디오 제작 계약]") < prompt.index("[사용자 요청]\n")  # versions.REQUEST_MARKER 앞
    root = game_dir("wk-studio")
    assert (root / ENTRY).is_file() and (root / ".claude/skills" / SKILL_NAME / "SKILL.md").is_file()
    assert (root / ENTRY).read_text() == (studio.KIT_SRC / "bin" / "studio.mjs").read_text()
    out = await asyncio.to_thread(subprocess.run, ["node", ENTRY, "help"], cwd=root, capture_output=True, text=True, check=False)
    assert out.returncode == 0 and "gate" in out.stdout
    status = (await asyncio.to_thread(subprocess.run, ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=False)).stdout
    assert SKILL_NAME not in status  # 저장소를 더럽히지 않는다
    await wait_run(client, auth, req["run_id"])
    ver = (await ask(client, auth, game["id"], kind="verify", instruction="확인")).json()
    vprompt = (await client.get(f"/api/runs/{ver['run_id']}", headers=auth)).json()["prompt"]
    assert "독립 검증이므로" in vprompt and "독립 검증만 수행한다" in vprompt
    await wait_run(client, auth, ver["run_id"])
