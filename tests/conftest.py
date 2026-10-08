"""통합 테스트 공통 설정. env 는 ttakkari 를 import 하기 전에 먼저 정해야 한다(get_settings 는 lru_cache)."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

_BASE = Path(tempfile.mkdtemp(prefix="ttk-it-")).resolve()
atexit.register(shutil.rmtree, _BASE, ignore_errors=True)
HOME = _BASE / "home"
ALLOWED = _BASE / "allowed"
OUTSIDE = _BASE / "outside"
for _d in (HOME, ALLOWED, OUTSIDE):
    _d.mkdir()
FAKES = Path(__file__).parent / "fake_agents"
AGENT_LOG = _BASE / "agent-log.jsonl"
TEST_DB = "postgresql+asyncpg://localhost/ttakkari_test"

os.environ.update({
    "TTAKKARI_ENV": "dev",
    "TTAKKARI_DATABASE_URL": TEST_DB,
    "TTAKKARI_HOME": str(HOME),
    "TTAKKARI_ALLOWED_ROOTS": json.dumps([str(ALLOWED)]),
    "TTAKKARI_JWT_SECRET": "test-secret-test-secret-test-secret-0123456789",
    "TTAKKARI_CLAUDE_BIN": str(FAKES / "fake_claude.py"),
    "TTAKKARI_CODEX_BIN": str(FAKES / "fake_codex.py"),
    "TTAKKARI_RUN_TIMEOUT_SECONDS": "30",
    "TTAKKARI_CANCEL_GRACE_SECONDS": "1",
    "TTAKKARI_CORS_ORIGINS": '["http://localhost:5173"]',
    # 매니저가 TTAKKARI_* 를 자식 env 에서 지우므로 접두사가 다른 이름을 쓴다.
    "FAKE_AGENT_LOG": str(AGENT_LOG),
})

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ttakkari.config import get_settings

get_settings.cache_clear()
assert str(get_settings().database_url).endswith("/ttakkari_test"), "테스트는 ttakkari_test DB 만 쓴다"

from ttakkari.db import sessionmaker
from ttakkari.main import app
from ttakkari.models import Base, Credential
from ttakkari.runs.manager import manager
from ttakkari.security.auth import issue_access_token, limiter
from ttakkari.security.passwords import hash_password

PASSWORD = "correct-horse-battery"


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def _schema_and_app():
    eng = create_async_engine(TEST_DB)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()
    async with app.router.lifespan_context(app):
        yield


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _clean(_schema_and_app):
    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with sessionmaker()() as db:
        await db.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        db.add(Credential(id=1, password_hash=hash_password(PASSWORD)))
        await db.commit()
    limiter.reset()
    AGENT_LOG.unlink(missing_ok=True)
    yield
    # 남은 가짜 에이전트(SLEEP)가 다음 테스트로 새지 않게 정리한다.
    for rid in list(manager._handles):
        await manager.cancel(rid)
    for h in list(manager._handles.values()):
        try:
            await h.task
        except BaseException:  # noqa: BLE001, S110
            pass


@pytest_asyncio.fixture(loop_scope="session")
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def auth() -> dict[str, str]:
    token, _ = issue_access_token()
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def make_dir():
    """allowed_roots 안에 새 디렉터리를 만든다."""
    def _make(name: str | None = None) -> Path:
        d = ALLOWED / (name or f"ws-{uuid.uuid4().hex[:8]}")
        d.mkdir(parents=True)
        return d
    return _make


@pytest.fixture
def outside_dir() -> Path:
    d = OUTSIDE / uuid.uuid4().hex[:8]
    d.mkdir(parents=True)
    return d


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
        capture_output=True, text=True).stdout.strip()


@pytest.fixture
def make_git_dir(make_dir):
    def _make() -> Path:
        d = make_dir()
        git(d, "init", "-q", "-b", "main")
        (d / "README.md").write_text("hello\n")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "init")
        return d
    return _make


@pytest_asyncio.fixture(loop_scope="session")
async def new_session(client, auth, make_dir):
    """워크스페이스 + 세션 생성 헬퍼. 기본은 일반 디렉터리."""
    async def _new(root: Path | None = None, engine: str = "claude") -> tuple[dict, dict]:
        root = root or make_dir()
        r = await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root),
                                                      "default_engine": engine}, headers=auth)
        assert r.status_code == 201, r.text
        ws = r.json()
        r = await client.post("/api/sessions", json={"workspace_id": ws["id"]}, headers=auth)
        assert r.status_code == 201, r.text
        return ws, r.json()
    return _new


def agent_calls() -> list[dict]:
    if not AGENT_LOG.exists():
        return []
    return [json.loads(line) for line in AGENT_LOG.read_text().splitlines() if line.strip()]


@pytest_asyncio.fixture(loop_scope="session")
async def client_500():
    """서버 예외를 raise 하지 않고 500 응답으로 돌려주는 클라이언트(버그 관찰용)."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
