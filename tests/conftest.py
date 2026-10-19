"""통합 테스트 공통 설정. env 는 ttakkari 를 import 하기 전에 먼저 정해야 한다(get_settings 는 lru_cache)."""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import subprocess
import tempfile
import uuid
import warnings
from pathlib import Path

from sqlalchemy.engine import make_url

_BASE = Path(tempfile.mkdtemp(prefix="ttk-it-")).resolve()
atexit.register(shutil.rmtree, _BASE, ignore_errors=True)
HOME = _BASE / "home"
ALLOWED = _BASE / "allowed"
OUTSIDE = _BASE / "outside"
for _d in (HOME, ALLOWED, OUTSIDE, ALLOWED / "default-root", ALLOWED / "default-docs", ALLOWED / "games"):
    _d.mkdir()
FAKES = Path(__file__).parent / "fake_agents"
AGENT_LOG = _BASE / "agent-log.jsonl"
# 동시에 여러 작업이 pytest 를 돌릴 수 있다. TTAKKARI_TEST_DB 를 지정하지 않으면 세션 픽스처가
# 프로세스별 고유 DB(ttakkari_test_<pid>_<난수>)를 만들어 쓰고 끝나면 지운다. 같은 기본 DB 를
# 공유하면 다른 프로세스의 TRUNCATE 와 엇갈려 sessions_workspace_id_fkey 위반·조회 404 같은
# 부분 실행 깨짐이 난다. 명시 지정은 주인이 관리하는 공유 계약으로 그대로 쓴다. 운영 DB(ttakkari)는 막는다.
TEST_DB_EXPLICIT = "TTAKKARI_TEST_DB" in os.environ
TEST_DB = os.environ.get("TTAKKARI_TEST_DB", "postgresql+asyncpg://localhost/ttakkari_test")
_PROCESS_DB_NAME: str | None = None  # 이 프로세스가 만든 고유 테스트 DB. 명시 지정이면 None.


def assert_test_db_isolated(database_url: str) -> None:
    """Reject the production database regardless of URL query parameters."""
    if make_url(database_url).database == "ttakkari":
        raise AssertionError("운영 DB 로 테스트하지 않는다")


assert_test_db_isolated(TEST_DB)

os.environ.update({
    "TTAKKARI_NO_ENV_FILE": "1",
    "TTAKKARI_ENV": "dev",
    "TTAKKARI_DATABASE_URL": TEST_DB,
    "TTAKKARI_HOME": str(HOME),
    "TTAKKARI_ALLOWED_ROOTS": json.dumps([str(ALLOWED)]),
    "TTAKKARI_DEFAULT_WORKSPACE_ROOT": str(ALLOWED / "default-root"),
    "TTAKKARI_GAMES_ROOT": str(ALLOWED / "games"),
    "TTAKKARI_GAME_AUTO_BUILD": "false",  # 게임 요청 테스트가 격리 빌드를 몰래 띄우지 않게 한다. 자동 빌드 테스트만 켠다
    "TTAKKARI_TRANSFER_CHUNK_BYTES": "1024",
    "TTAKKARI_REMINDER_SCAN_SECONDS": "0",
    "TTAKKARI_USAGE_POLL_SECONDS": "0",
    "TTAKKARI_RESOURCE_SAMPLE_SECONDS": "0",
    "TTAKKARI_LEAD_ARCHIVE_SCAN_SECONDS": "0",
    "TTAKKARI_OPS_LOG_SYNC_SECONDS": "0",
    "TTAKKARI_CONTEXT_EMBEDDER": "hash",
    "TTAKKARI_CLOVANOTE_POLL_SECONDS": "0",
    "TTAKKARI_LOCAL_STT_MODEL_PATH": str(ALLOWED / "missing-test-model"),
    "TTAKKARI_CONTEXT_SOURCE_ROOTS": json.dumps([str(ALLOWED)]),
    "TTAKKARI_DEFAULT_WRITABLE_ROOTS": json.dumps([str(ALLOWED / "default-docs"), str(ALLOWED / "default-missing")]),
    "TTAKKARI_JWT_SECRET": "test-secret-test-secret-test-secret-0123456789",
    "TTAKKARI_CLAUDE_BIN": str(FAKES / "fake_claude.py"),
    "TTAKKARI_CODEX_BIN": str(FAKES / "fake_codex.py"),
    "TTAKKARI_ANTIGRAVITY_MODEL_PROBE": "false",
    "TTAKKARI_RUN_TIMEOUT_SECONDS": "30",
    "TTAKKARI_CANCEL_GRACE_SECONDS": "1",
    "TTAKKARI_CORS_ORIGINS": '["http://localhost:5173"]',
    # 접수 상주 풀은 test_desk 의 풀 테스트에서만 켠다. 나머지는 지금까지처럼 1회 실행(argv 검증)을 본다.
    "TTAKKARI_DESK_WARM_POOL": "false",
    "TTAKKARI_DESK_QWEN_ENABLED": "false",
    # 매니저가 TTAKKARI_* 를 자식 env 에서 지우므로 접두사가 다른 이름을 쓴다.
    "FAKE_AGENT_LOG": str(AGENT_LOG),
})

import asyncpg
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ttakkari.config import get_settings

get_settings.cache_clear()
assert str(get_settings().database_url) == TEST_DB, "테스트 DB 설정이 적용되지 않았다"

from ttakkari.api.assistant import wait_indexing
from ttakkari.db import dispose_engine, sessionmaker
from ttakkari.main import app
from ttakkari.models import Base, Credential
from ttakkari.runs import desk_pool, profiles, usage_store
from ttakkari.runs.manager import _background, manager
from ttakkari.security.auth import issue_access_token, limiter
from ttakkari.security.passwords import hash_password

PASSWORD = "correct-horse-battery"


def _admin_kwargs() -> dict:
    """기본 테스트 DB 와 같은 서버의 postgres 관리 DB 접속 정보."""
    url = make_url(TEST_DB)
    kw: dict = {"database": "postgres", "host": url.host or "localhost"}
    if url.port:
        kw["port"] = url.port
    if url.username:
        kw["user"] = url.username
    if url.password:
        kw["password"] = url.password
    return kw


async def _use_process_database() -> None:
    """명시 지정이 아니면 이 프로세스 전용 테스트 DB 를 만들어 TEST_DB·env·settings 에 반영한다.

    엔진은 이 픽스처 이후에 처음 만들어지므로(lazy sessionmaker) 여기서 URL 을 바꿔도 안전하다.
    만들지 못하면(권한·접속 불가) 기본 DB 로 경고와 함께 진행한다. 접속 자체가 안 되면
    어차피 모든 DB 테스트가 setup 에서 실패하므로 격리 거짓말을 하지 않는다.
    """
    global TEST_DB, _PROCESS_DB_NAME
    if TEST_DB_EXPLICIT or _PROCESS_DB_NAME is not None:
        return
    name = f"ttakkari_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        admin = await asyncpg.connect(**_admin_kwargs())
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
        finally:
            await admin.close()
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"고유 테스트 DB 를 만들지 못해 기본 DB 를 공유한다: {type(e).__name__}: {e}", stacklevel=2)
        return
    _PROCESS_DB_NAME = name
    TEST_DB = str(make_url(TEST_DB).set(database=name))
    assert_test_db_isolated(TEST_DB)
    os.environ["TTAKKARI_DATABASE_URL"] = TEST_DB
    get_settings.cache_clear()
    assert str(get_settings().database_url) == TEST_DB, "고유 테스트 DB 설정이 적용되지 않았다"


async def _drop_process_database() -> None:
    """_use_process_database 가 만든 DB 를 정리한다. 실패해도 세션 결과를 깨지 않는다."""
    global _PROCESS_DB_NAME
    if _PROCESS_DB_NAME is None:
        return
    name, _PROCESS_DB_NAME = _PROCESS_DB_NAME, None
    try:
        await dispose_engine()
        admin = await asyncpg.connect(**_admin_kwargs())
        try:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        finally:
            await admin.close()
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"테스트 DB {name} 정리에 실패했다(수동 삭제 필요): {type(e).__name__}: {e}", stacklevel=2)


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def _schema_and_app():
    await _use_process_database()
    eng = create_async_engine(TEST_DB)
    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()
    async with app.router.lifespan_context(app):
        yield
    await _drop_process_database()


@pytest.fixture(autouse=True)
def _no_muse_ping():
    # Muse 관측 핑은 실제 Muse 를 띄운다. 시험은 켜진 계정이 있어도 핑하지 않는다(tests/test_muse_observation_ping.py 만 켠다).
    # NOTE: autouse 에서 monkeypatch 를 받으면 monkeypatch 가 다른 autouse 픽스처(예: settings fields_set 복원)보다 먼저
    # 만들어져 늦게 정리되므로 그 복원을 되돌린다. 그래서 값을 직접 저장·복원한다.
    from ttakkari.runs import usage
    before = usage.MUSE_PING
    usage.MUSE_PING = False
    yield
    usage.MUSE_PING = before


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _clean(_schema_and_app):
    await profiles.flush_persist()
    await usage_store.flush()
    usage_store.reset()
    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with sessionmaker()() as db:
        await db.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        db.add(Credential(id=1, password_hash=hash_password(PASSWORD)))
        await db.commit()
    limiter.reset()
    profiles.reset_cooldowns()
    AGENT_LOG.unlink(missing_ok=True)
    yield
    # 남은 가짜 에이전트(SLEEP)가 다음 테스트로 새지 않게 정리한다. 취소가 결과 보고 Run 을 다시 띄울 수 있어
    # (ADR 0006) 실행 중인 것과 백그라운드 디스패치가 모두 없어질 때까지 반복한다.
    # 지식 색인(백그라운드)이 다음 테스트의 TRUNCATE 와 겹치면 교착이 난다.
    await wait_indexing()
    await desk_pool.shutdown()
    # 한도 대기 중인 Run 의 재개 타이머가 다음 테스트로 새지 않게 끈다.
    if manager._restart_waker is not None:
        manager._restart_waker.cancel()
        try:
            await manager._restart_waker
        except asyncio.CancelledError:
            pass
        manager._restart_waker = None
    for rid in list(manager._wakers) + list(manager._waiting):
        manager._disarm(rid)
    for _ in range(20):
        if not manager._handles and not _background:
            break
        for rid in list(manager._handles):
            await manager.cancel(rid)
        for h in list(manager._handles.values()):
            try:
                await h.task
            except BaseException:  # noqa: BLE001, S110
                pass
        for t in list(_background):
            try:
                await t
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
