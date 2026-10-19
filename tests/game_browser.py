"""게임 브라우저 검증(G13·G14) 공용 도구: Playwright(node) 위치 확인, 별도 프로세스 게임 origin, node 시나리오 실행.

브라우저 자동화는 node Playwright 로 한다. 이 저장소의 `.browser-runtime`(@playwright/mcp 의존)에 설치된 playwright 를 쓰고,
다른 위치는 `TTAKKARI_PLAYWRIGHT_DIR`(node_modules 를 가진 디렉터리)로 지정한다. 없으면 호출하는 테스트가 사유와 함께 skip 한다.
`TTAKKARI_REQUIRE_GAME_BROWSER=1` 이면 skip 대신 실패시켜 이 Mac 에서 몰래 건너뛰는 일을 막는다.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

BACK = Path(__file__).resolve().parent.parent
SCENARIOS = Path(__file__).parent / "game_smoke"
PLAYWRIGHT_DIR = Path(os.environ.get("TTAKKARI_PLAYWRIGHT_DIR") or BACK / ".browser-runtime")
RESULT_MARK = "@@RESULT@@"


def browser_problem() -> str | None:
    if shutil.which("node") is None:
        return "node 가 없다"
    if not (PLAYWRIGHT_DIR / "node_modules" / "playwright").is_dir():
        return f"playwright(node) 가 {PLAYWRIGHT_DIR}/node_modules 에 없다(TTAKKARI_PLAYWRIGHT_DIR 로 지정)"
    return None


def require_browser() -> None:
    problem = browser_problem()
    if problem is None:
        return
    if os.environ.get("TTAKKARI_REQUIRE_GAME_BROWSER") == "1":
        pytest.fail(f"TTAKKARI_REQUIRE_GAME_BROWSER=1 인데 브라우저 자동화를 쓸 수 없다: {problem}", pytrace=False)
    pytest.skip(f"브라우저 자동화 필요: {problem}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def serve_games(port: int, frame_ancestors: list[str]) -> Iterator[subprocess.Popen]:
    """`ttakkari serve-games` 와 같은 앱(games.origin:app)을 별도 프로세스로 띄운다. DB·세션·쿠키 설정 없이 서명 키만 받는다."""
    keep = {"PATH", "HOME", "TMPDIR", "TTAKKARI_HOME", "TTAKKARI_JWT_SECRET", "TTAKKARI_NO_ENV_FILE", "TTAKKARI_ENV", "TTAKKARI_CORS_ORIGINS",
            "TTAKKARI_ALLOWED_ROOTS", "TTAKKARI_GAMES_ROOT"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["TTAKKARI_DATABASE_URL"] = "postgresql+asyncpg://localhost/does_not_exist"
    env["TTAKKARI_GAME_FRAME_ANCESTORS"] = json.dumps(frame_ancestors)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "ttakkari.games.origin:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(150):
            if proc.poll() is not None:
                raise AssertionError("serve-games 가 바로 종료됐다: " + (proc.stderr.read().decode() if proc.stderr else ""))
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("serve-games 가 포트를 열지 않았다")
        yield proc
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(10)


def run_scenario(script: str, config: dict, *, timeout: int = 240) -> dict:
    """tests/game_smoke/<script> 를 node 로 실행하고 마지막 @@RESULT@@ JSON 을 돌려준다."""
    cfg = {"playwright_dir": str(PLAYWRIGHT_DIR), **config}
    proc = subprocess.run(["node", str(SCENARIOS / script), json.dumps(cfg)], capture_output=True, text=True, timeout=timeout, check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(RESULT_MARK)]
    assert lines, f"시나리오가 결과를 내지 않았다(exit {proc.returncode})\nstdout: {proc.stdout[-800:]}\nstderr: {proc.stderr[-1500:]}"
    return json.loads(lines[-1][len(RESULT_MARK):])
