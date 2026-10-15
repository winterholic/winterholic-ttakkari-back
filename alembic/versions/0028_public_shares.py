"""Approved public capability snapshots, plus transactional source revocation."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None

# Frozen here: deletion/scope/reparent cannot revive the same secret, including
# writes from analysis workers or SQL outside the owner HTTP handlers.
TRIGGER_STATEMENTS = ["""
CREATE FUNCTION revoke_bound_public_shares() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF TG_ARGV[0] = 'meeting' THEN
      IF NEW.scope IS NOT DISTINCT FROM OLD.scope THEN RETURN NEW; END IF;
    ELSIF TG_ARGV[0] = 'item' THEN
      IF NEW.meeting_id = OLD.meeting_id AND NEW.kind = OLD.kind THEN RETURN NEW; END IF;
    ELSIF TG_ARGV[0] = 'job' THEN
      IF NEW.meeting_id = OLD.meeting_id AND NEW.parent_job_id IS NOT DISTINCT FROM OLD.parent_job_id
         AND NEW.status = OLD.status THEN RETURN NEW; END IF;
    END IF;
  END IF;
  UPDATE public_shares SET state='revoked', revoked_at=clock_timestamp(),
    revoked_reason='source_unavailable', secret_hash=NULL, snapshot=NULL, source_bindings='[]'::jsonb
  WHERE state='active' AND resource_type='meeting' AND (
    (TG_ARGV[0] = 'meeting' AND resource_id=OLD.id)
    OR source_bindings @> jsonb_build_array(jsonb_build_object('type', TG_ARGV[0], 'id', OLD.id::text))
  );
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$
""",
"CREATE TRIGGER public_share_meeting_delete BEFORE DELETE ON meetings FOR EACH ROW EXECUTE FUNCTION revoke_bound_public_shares('meeting')",
"CREATE TRIGGER public_share_meeting_scope BEFORE UPDATE OF scope ON meetings FOR EACH ROW EXECUTE FUNCTION revoke_bound_public_shares('meeting')",
"CREATE TRIGGER public_share_item_delete BEFORE DELETE ON meeting_items FOR EACH ROW EXECUTE FUNCTION revoke_bound_public_shares('item')",
"CREATE TRIGGER public_share_item_parent BEFORE UPDATE OF meeting_id, kind ON meeting_items FOR EACH ROW EXECUTE FUNCTION revoke_bound_public_shares('item')",
"CREATE TRIGGER public_share_job_delete BEFORE DELETE ON local_stt_jobs FOR EACH ROW EXECUTE FUNCTION revoke_bound_public_shares('job')",
"CREATE TRIGGER public_share_job_parent BEFORE UPDATE OF meeting_id, parent_job_id, status ON local_stt_jobs FOR EACH ROW EXECUTE FUNCTION revoke_bound_public_shares('job')"]


def upgrade():
    op.create_table('public_shares',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('resource_type', sa.String(20), nullable=False),
        sa.Column('resource_id', sa.Uuid(), nullable=False),
        sa.Column('created_by', sa.String(10), nullable=False),
        sa.Column('secret_hash', sa.String(64)),
        sa.Column('state', sa.String(10), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True)),
        sa.Column('revoked_reason', sa.String(30)),
        sa.Column('source_scope', sa.String(20), nullable=False),
        sa.Column('selection', JSONB(), nullable=False),
        sa.Column('download_formats', JSONB(), nullable=False),
        sa.Column('publication_revision', sa.Integer(), nullable=False),
        sa.Column('source_revision', sa.String(71), nullable=False),
        sa.Column('snapshot_schema_version', sa.Integer(), nullable=False),
        sa.Column('snapshot', JSONB()),
        sa.Column('source_bindings', JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('uq_public_shares_active_root', 'public_shares', ['resource_type', 'resource_id'],
                    unique=True, postgresql_where=sa.text("state = 'active'"))
    for statement in TRIGGER_STATEMENTS:
        op.execute(sa.text(statement))


def downgrade():
    for name, table in [('meeting_delete', 'meetings'), ('meeting_scope', 'meetings'),
        ('item_delete', 'meeting_items'), ('item_parent', 'meeting_items'),
        ('job_delete', 'local_stt_jobs'), ('job_parent', 'local_stt_jobs')]:
        op.execute(sa.text(f'DROP TRIGGER public_share_{name} ON {table}'))
    op.execute(sa.text('DROP FUNCTION revoke_bound_public_shares()'))
    op.drop_index('uq_public_shares_active_root', table_name='public_shares')
    op.drop_table('public_shares')
