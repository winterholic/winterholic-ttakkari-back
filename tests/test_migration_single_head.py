"""Alembic 마이그레이션 그래프가 head 하나인지 검사한다.

두 세션이 같은 down_revision 에서 마이그레이션을 따로 만들면 head 가 둘이 되어 배포 게이트의 세대 계약 검사
(`get_current_head`)가 MultipleHeads 로 깨진다. contract 마크로 묶여 push 전 사전 검증에 항상 포함된다.
"""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[1]


def test_alembic_has_exactly_one_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, (
        f"마이그레이션 head 가 {len(heads)}개다: {sorted(heads)}. 한쪽 down_revision 을 다른 head 뒤로 옮겨 합친다."
    )


def test_delivery_models_are_registered_in_alembic_metadata():
    from ttakkari.delivery_models import ChangeSet
    from ttakkari.models import Base

    env_source = (ROOT / "alembic/env.py").read_text(encoding="utf-8")
    assert "import ttakkari.delivery_models" in env_source
    assert Base.metadata.tables[ChangeSet.__tablename__] is ChangeSet.__table__
