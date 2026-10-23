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
from pathlib import Path

import pg_sandbox
from git_isolation import git_argv, isolated_git_env, scrub_process_env
from sqlalchemy.engine import make_url

# pre-push 훅에서 도는 pytest 는 GIT_DIR 등을 물려받는다. 모든 subprocess 가 같은 오염을 받지 않도록 먼저 지운다.
scrub_process_env()

_BASE = Path(tempfile.mkdtemp(prefix="ttk-it-")).resolve()
atexit.register(shutil.rmtree, _BASE, ignore_errors=True)
HOME = _BASE / "home"
ALLOWED = _BASE / "allowed"
OUTSIDE = _BASE / "outside"
for _d in (HOME, ALLOWED, OUTSIDE, ALLOWED / "default-root", ALLOWED / "default-docs", ALLOWED / "games"):
    _d.mkdir()
FAKES = Path(__file__).parent / "fake_agents"
AGENT_LOG = _BASE / "agent-log.jsonl"
# 동시에 여러 작업이 pytest 를 돌릴 수 있다. TTAKKARI_TEST_DB 를 지정하지 않으면 이 프로세스가
# /tmp/ttakkari-test-<pid>-<난수>/ 아래에 임시 PostgreSQL 클러스터를 띄워 쓰고 끝나면 폴더째 지운다.
# 공유 PostgreSQL 에는 DB 를 만들지 않으므로 프로세스가 비정상 종료돼도 공유 서버에 잔재가 없고,
# 남은 폴더는 다음 세션 시작 때 소유 pid 가 죽은 것만 정리된다(tests/pg_sandbox.py).
# host="localhost" 직결 마이그레이션 시험은 asyncpg 패치로, alembic 자식은 PGPORT 포함 URL
# (pg_sandbox.localhost_database_url)로 같은 클러스터로 간다. 명시 지정은 주인이 관리하는 공유
# 계약으로 그대로 쓴다. 운영 DB(ttakkari)는 막는다. env 는 ttakkari import 전에 정해야 하므로
# 클러스터는 import 시점에 띄운다.
TEST_DB_EXPLICIT = "TTAKKARI_TEST_DB" in os.environ
if TEST_DB_EXPLICIT:
    TEST_DB = os.environ["TTAKKARI_TEST_DB"]
    import asyncpg

    from ttakkari.runs.test_database import patch_asyncpg_test_port

    patch_asyncpg_test_port(asyncpg, TEST_DB)
else:
    PG_CLUSTER = pg_sandbox.start_cluster()
    pg_sandbox.use_cluster_env(PG_CLUSTER)
    pg_sandbox.patch_asyncpg_for_cluster(PG_CLUSTER)
    TEST_DB = PG_CLUSTER.create_database("ttakkari_test")


def assert_test_db_isolated(database_url: str) -> None:
    """Reject the production database regardless of URL query parameters."""
    if make_url(database_url).database == "ttakkari":
        raise AssertionError("운영 DB 로 테스트하지 않는다")


assert_test_db_isolated(TEST_DB)
os.environ["TTAKKARI_TEST_DB"] = TEST_DB
os.environ["TTAKKARI_TEST_RUNNER"] = "1"

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
    "TTAKKARI_COLLECTOR_CONTROL_ENABLED": "false",
    "TTAKKARI_KB_SEMANTIC_ENABLED": "false",
    "TTAKKARI_USAGE_POLL_SECONDS": "0",
    "TTAKKARI_QWEN_FLASH_POLL_SECONDS": "0",
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

import httpx
import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ttakkari.config import get_settings

get_settings.cache_clear()
assert str(get_settings().database_url) == TEST_DB, "테스트 DB 설정이 적용되지 않았다"

from ttakkari.api.assistant import wait_indexing
from ttakkari.db import sessionmaker
from ttakkari.main import app
from ttakkari.models import Base, Credential
from ttakkari.runs import desk_pool, profiles, usage_store
from ttakkari.runs.manager import _background, manager
from ttakkari.security import passwords as _passwords
from ttakkari.security.auth import issue_access_token, limiter
from ttakkari.security.passwords import hash_password

PASSWORD = "correct-horse-battery"

# 시험에서만 argon2 비용을 낮춘다(기본 t=3, m=64MiB, p=4 는 해시 한 번에 약 26ms). 운영 해시 설정은 passwords.py 가 그대로
# 갖고 있고, 이 대입은 pytest 프로세스 안에서만 일어난다(tests/test_fixture_reset.py 가 운영 값이 그대로임을 고정한다).
# 낮은 비용으로 만든 해시도 운영 해셔가 파라미터를 읽어 그대로 검증한다.
_passwords._hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
# 시험마다 같은 계정을 다시 넣으므로 해시는 세션에서 한 번만 만든다.
PASSWORD_HASH = hash_password(PASSWORD)

_TABLE_NAMES = [t.name for t in Base.metadata.sorted_tables]
_ALL_TABLES_SQL = ", ".join(f'"{n}"' for n in _TABLE_NAMES)
# 시험이 건드린 테이블 = 행이 있는 테이블 + 행을 지웠어도 identity/serial 시퀀스가 전진한 테이블.
# 쓰기를 추적하지 않고 DB 상태를 그대로 읽으므로, 어떤 경로(ORM·원시 SQL·다른 연결)로 썼든 놓치지 않는다.
_DIRTY_SQL = " UNION ".join([
    *(f"SELECT '{n}' AS n WHERE EXISTS (SELECT 1 FROM \"{n}\")" for n in _TABLE_NAMES),
    (
        "SELECT t.relname AS n FROM pg_class t "
        "JOIN pg_depend d ON d.refobjid = t.oid AND d.deptype IN ('a', 'i') "
        "JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S' "
        "JOIN pg_sequences q ON q.sequencename = s.relname "
        "WHERE q.last_value IS NOT NULL"
    ),
])
# TTAKKARI_TEST_FULL_TRUNCATE=1 이면 예전처럼 매번 모든 테이블을 비운다(되돌리기·대조용).
FULL_TRUNCATE = os.environ.get("TTAKKARI_TEST_FULL_TRUNCATE") == "1"
RESET_STATS = {"selective": 0, "tables": 0, "full": 0, "fallback": 0}


async def dirty_tables(db) -> set[str]:
    rows = (await db.execute(text(_DIRTY_SQL))).all()
    return {r[0] for r in rows} & set(_TABLE_NAMES)


async def reset_database(db) -> None:
    """모델 테이블을 시험 시작 상태(비어 있고 시퀀스 처음, 계정 1개)로 되돌린다.

    건드린 테이블만 비운다. 건드린 테이블을 읽는 데 실패하면 예전처럼 전부 비운다(fail-safe).
    """
    # kb_* 원장은 BEFORE TRUNCATE 트리거로 TRUNCATE 가 막혀 있다(Alembic 0058). 시험 초기화만 트리거를 건너뛴다.
    await db.execute(text("SET session_replication_role = 'replica'"))
    names = _ALL_TABLES_SQL
    if FULL_TRUNCATE:
        RESET_STATS["full"] += 1
    else:
        try:
            # 실패한 SQL 은 트랜잭션을 중단시키므로 savepoint 안에서 읽는다.
            async with db.begin_nested():
                dirty = await dirty_tables(db)
            names = ", ".join(f'"{n}"' for n in sorted(dirty))
            RESET_STATS["selective"] += 1
            RESET_STATS["tables"] += len(dirty)
        except Exception:  # noqa: BLE001
            RESET_STATS["fallback"] += 1
            names = _ALL_TABLES_SQL
    if names:
        await db.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    await db.execute(text("SET session_replication_role = 'origin'"))
    db.add(Credential(id=1, password_hash=PASSWORD_HASH))
    await db.commit()


def pytest_terminal_summary(terminalreporter):
    s = RESET_STATS
    if s["selective"] or s["full"] or s["fallback"]:
        mean = s["tables"] / s["selective"] if s["selective"] else 0
        terminalreporter.write_line(
            f"db reset: {s['selective']} selective (avg {mean:.1f}/{len(_TABLE_NAMES)} tables), "
            f"{s['full']} forced full, {s['fallback']} fell back to full")


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def _schema_and_app():
    eng = create_async_engine(TEST_DB)
    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()
    async with app.router.lifespan_context(app):
        yield


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
    async with sessionmaker()() as db:
        await reset_database(db)
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
        git_argv(cwd, *args), cwd=cwd, env=isolated_git_env(), check=True,
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
