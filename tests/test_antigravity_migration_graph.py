from alembic.config import Config
from alembic.script import ScriptDirectory


def test_antigravity_usage_migration_follows_public_shares_in_one_graph():
    script = ScriptDirectory.from_config(Config("alembic.ini"))

    assert script.get_heads() == ["0029_antigravity_usage_credits"]
    quota = script.get_revision("0029_antigravity_usage_credits")
    assert quota.down_revision == "0028"
