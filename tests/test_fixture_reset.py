"""공통 fixture(tests/conftest.py)의 시험당 고정비 절감이 격리와 운영 설정을 해치지 않는지 고정한다.

1. 시험에서만 낮춘 argon2 비용이 운영 해시 설정(src/ttakkari/security/passwords.py)을 바꾸지 않았다.
2. 건드린 테이블만 비워도 어떤 쓰기 경로(행·시퀀스)든 다음 시험으로 새지 않는다.
3. 건드린 테이블을 읽지 못하면 예전처럼 전부 비운다.
"""
from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import conftest
from argon2 import Type
from sqlalchemy import text

from ttakkari.db import sessionmaker
from ttakkari.models import Credential
from ttakkari.security import passwords

PASSWORD_MODULE = Path(passwords.__file__)
CRED = Credential.__tablename__


def _load_fresh_passwords_module():
    """pytest 가 대입한 _hasher 와 무관하게 파일 그대로의 모듈을 새로 읽는다."""
    spec = importlib.util.spec_from_file_location("_production_passwords_probe", PASSWORD_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_production_password_hashing_parameters_are_unchanged():
    prod = _load_fresh_passwords_module()
    params = prod._hasher._parameters
    # argon2-cffi 기본(RFC 9106 low-memory). 라이브러리 기본이 바뀌면 운영 해시 비용도 바뀌므로 여기서 알아챈다.
    assert (params.type, params.time_cost, params.memory_cost, params.parallelism, params.hash_len, params.salt_len) == (
        Type.ID, 3, 65536, 4, 32, 16)
    assert prod.hash_password(conftest.PASSWORD).startswith("$argon2id$v=19$m=65536,t=3,p=4$")


def test_production_hasher_is_constructed_with_defaults_and_nothing_else_overrides_it():
    tree = ast.parse(PASSWORD_MODULE.read_text())
    assigns = [n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "_hasher" for t in n.targets)]
    assert len(assigns) == 1
    call = assigns[0].value
    assert isinstance(call, ast.Call) and not call.args and not call.keywords, "운영 해셔는 인자 없이 만든다"

    src_root = PASSWORD_MODULE.parents[1]
    offenders = [str(path.relative_to(src_root)) for path in src_root.rglob("*.py") if path != PASSWORD_MODULE
                 and re.search(r"\b_hasher\s*=|passwords\._hasher|PasswordHasher\(", path.read_text())]
    assert offenders == [], "운영 코드가 해셔를 바꾸거나 따로 만들면 안 된다"


def test_test_hasher_is_cheap_but_its_hashes_verify_under_the_production_hasher():
    assert passwords._hasher is not _load_fresh_passwords_module()._hasher
    cheap = passwords._hasher._parameters
    assert cheap.time_cost == 1 and cheap.memory_cost == 8  # 시험 전용(conftest)
    assert passwords.verify_password(conftest.PASSWORD_HASH, conftest.PASSWORD)
    assert not passwords.verify_password(conftest.PASSWORD_HASH, "wrong-password-value")
    prod = _load_fresh_passwords_module()
    # 운영 해셔는 해시에 적힌 파라미터로 검증하므로 낮은 비용 해시도 읽을 수 있고, 다시 해시할 대상으로 본다.
    assert prod.verify_password(conftest.PASSWORD_HASH, conftest.PASSWORD)
    assert prod.needs_rehash(conftest.PASSWORD_HASH)


def test_dirty_query_names_every_model_table():
    assert conftest._TABLE_NAMES
    for name in conftest._TABLE_NAMES:
        assert f'FROM "{name}"' in conftest._DIRTY_SQL


async def test_clean_state_has_only_the_seeded_credential():
    async with sessionmaker()() as db:
        assert await conftest.dirty_tables(db) == {CRED}
        assert (await db.execute(text("SELECT password_hash FROM credentials"))).scalar_one() == conftest.PASSWORD_HASH


async def _owned_sequences(db) -> list[tuple[str, str]]:
    rows = await db.execute(text(
        "SELECT t.relname, s.relname FROM pg_class t "
        "JOIN pg_depend d ON d.refobjid = t.oid AND d.deptype IN ('a', 'i') "
        "JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S' ORDER BY 1"))
    return [(r[0], r[1]) for r in rows if r[0] in conftest._TABLE_NAMES]


_FILLERS = {
    "boolean": "false", "uuid": "gen_random_uuid()", "jsonb": "'{}'::jsonb", "json": "'{}'::json",
    "date": "current_date", "ARRAY": "'{}'",
}


def _filler(data_type: str) -> str | None:
    if data_type in _FILLERS:
        return _FILLERS[data_type]
    if data_type in ("text", "character varying", "character"):
        return "'x'"
    if data_type in ("integer", "bigint", "smallint", "numeric", "double precision", "real"):
        return "0"
    if data_type.startswith("timestamp"):
        return "now()"
    return None


async def _insert_minimal_row(db, table: str) -> bool:
    """필수 열만 형식에 맞는 값으로 채워 한 행을 넣는다(FK 는 replica 역할로 건너뜀). 못 넣으면 False."""
    cols = (await db.execute(text(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = :t AND is_nullable = 'NO' AND column_default IS NULL AND is_identity = 'NO' "
        "AND is_generated = 'NEVER'"), {"t": table})).all()
    values = [_filler(data_type) for _, data_type in cols]
    if any(v is None for v in values):
        return False
    names = ", ".join(f'"{name}"' for name, _ in cols)
    try:
        async with db.begin_nested():
            if cols:
                await db.execute(text(f'INSERT INTO "{table}" ({names}) VALUES ({", ".join(values)})'))
            else:
                await db.execute(text(f'INSERT INTO "{table}" DEFAULT VALUES'))
    except Exception:  # noqa: BLE001 CHECK·UNIQUE 제약으로 못 넣는 테이블은 건너뛴다
        return False
    return True


async def test_reset_clears_rows_and_sequences_whatever_wrote_them():
    async with sessionmaker()() as db:
        await db.execute(text("SET session_replication_role = 'replica'"))
        inserted = [name for name in conftest._TABLE_NAMES if name != CRED and await _insert_minimal_row(db, name)]
        await db.execute(text("SET session_replication_role = 'origin'"))
        # 행 없이 시퀀스만 전진한 테이블(롤백된 insert·삭제된 행이 남기는 흔적)
        sequences = [pair for pair in await _owned_sequences(db) if pair[0] not in inserted][:5]
        for _, seq in sequences:
            await db.execute(text(f'SELECT nextval(\'"{seq}"\')'))
        await db.execute(text("UPDATE credentials SET password_hash = 'tampered'"))
        await db.commit()
        # 대부분의 테이블에 실제로 쓰였는지 확인한다. 그래야 아래 검사가 많은 테이블에서 의미가 있다.
        assert len(inserted) >= len(conftest._TABLE_NAMES) // 3, inserted

        dirty = await conftest.dirty_tables(db)
        assert set(inserted) <= dirty
        assert {table for table, _ in sequences} <= dirty

        await conftest.reset_database(db)
        assert await conftest.dirty_tables(db) == {CRED}
        assert (await db.execute(text("SELECT password_hash FROM credentials"))).scalar_one() == conftest.PASSWORD_HASH
        for _, seq in sequences:  # RESTART IDENTITY: 다음 시험은 처음부터 시작한다
            assert (await db.execute(text(f'SELECT nextval(\'"{seq}"\')'))).scalar_one() == 1


async def test_reset_falls_back_to_truncating_everything_when_dirty_detection_fails(monkeypatch):
    monkeypatch.setattr(conftest, "_DIRTY_SQL", 'SELECT n FROM "table_that_does_not_exist"')
    before = dict(conftest.RESET_STATS)
    async with sessionmaker()() as db:
        await db.execute(text("UPDATE credentials SET password_hash = 'tampered'"))
        await db.commit()
        await conftest.reset_database(db)
    assert conftest.RESET_STATS["fallback"] == before["fallback"] + 1
    assert conftest.RESET_STATS["selective"] == before["selective"]
    monkeypatch.undo()
    async with sessionmaker()() as db:
        assert await conftest.dirty_tables(db) == {CRED}
        assert (await db.execute(text("SELECT password_hash FROM credentials"))).scalar_one() == conftest.PASSWORD_HASH


async def test_forced_full_truncate_switch_restores_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(conftest, "FULL_TRUNCATE", True)
    before = conftest.RESET_STATS["full"]
    async with sessionmaker()() as db:
        await db.execute(text("UPDATE credentials SET password_hash = 'tampered'"))
        await db.commit()
        await conftest.reset_database(db)
        assert (await db.execute(text("SELECT password_hash FROM credentials"))).scalar_one() == conftest.PASSWORD_HASH
    assert conftest.RESET_STATS["full"] == before + 1
