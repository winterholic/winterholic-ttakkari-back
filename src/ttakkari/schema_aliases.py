"""이름이 바뀌어 저장소에 없는 옛 alembic 리비전 ID를 계약이 보증한 ID로 고쳐 쓴다.

배포 감독은 후보 릴리스의 `ttakkari.cli migrate`를 부른다. 감독 코드는 릴리스보다 오래될 수
있으므로 정렬은 감독이 아니라 릴리스 쪽(alembic/env.py)에서 일어나야 어느 감독에서도 통한다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

log = logging.getLogger(__name__)
CONTRACT = Path(__file__).resolve().parents[2] / "deploy" / "generation-contract.json"


def load_aliases(path: Path = CONTRACT) -> dict[str, str]:
    try:
        aliases = json.loads(path.read_text()).get("schema_aliases", {})
    except (OSError, ValueError):
        return {}
    return aliases if isinstance(aliases, dict) else {}


def realign(connection: Connection, aliases: dict[str, str] | None = None) -> tuple[str, str] | None:
    """alembic_version 이 별칭이면 대상 ID로 바꾸고 즉시 커밋한다. 바꿨다면 (옛 ID, 새 ID).

    조회만 해도 SQLAlchemy 가 트랜잭션을 시작한다. 그대로 alembic 에 넘기면 외부 트랜잭션으로
    오해해 migration 을 커밋하지 않고 롤백하므로, 어느 경로로 끝나든 트랜잭션을 닫고 돌려준다.
    별칭이 아닌 값·빈 테이블·테이블 없음은 건드리지 않는다.
    """
    aliases = load_aliases() if aliases is None else aliases
    if not aliases:
        return None
    try:
        if not inspect(connection).has_table("alembic_version"):
            return None
        current = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        if len(current) != 1 or current[0] not in aliases:
            return None
        old, new = current[0], aliases[current[0]]
        connection.execute(text("UPDATE alembic_version SET version_num = :new WHERE version_num = :old"),
                           {"new": new, "old": old})
        connection.commit()
    finally:
        if connection.in_transaction():
            connection.rollback()
    log.warning("alembic_version %s realigned to %s (contract schema_aliases)", old, new)
    return old, new
