"""context sharing v2 credential metadata and durable revoke intent."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0080_ctx_share_v2_keys"
down_revision = "0079_context_sharing"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ctx_share_projects", sa.Column("revoke_state", sa.String(16), nullable=False, server_default="active"))
    op.add_column("ctx_share_projects", sa.Column("revoke_error", sa.String(64), nullable=True))
    op.add_column("ctx_share_keys", sa.Column("credential_type", sa.String(24), nullable=False,
                                                server_default="single_use_v1"))
    op.add_column("ctx_share_keys", sa.Column("snapshot_policy", sa.String(8), nullable=False, server_default="latest"))
    op.add_column("ctx_share_keys", sa.Column("pinned_snapshot_id", sa.String(36), nullable=True))
    op.add_column("ctx_share_keys", sa.Column("scopes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("ctx_share_keys", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ctx_share_keys", sa.Column("revoke_error", sa.String(64), nullable=True))
    op.execute("UPDATE ctx_share_keys SET scopes = '[]'::jsonb WHERE scopes IS NULL")


def downgrade():
    op.drop_column("ctx_share_keys", "revoke_error")
    op.drop_column("ctx_share_keys", "last_used_at")
    op.drop_column("ctx_share_keys", "scopes")
    op.drop_column("ctx_share_keys", "pinned_snapshot_id")
    op.drop_column("ctx_share_keys", "snapshot_policy")
    op.drop_column("ctx_share_keys", "credential_type")
    op.drop_column("ctx_share_projects", "revoke_error")
    op.drop_column("ctx_share_projects", "revoke_state")
