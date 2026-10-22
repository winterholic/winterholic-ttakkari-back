"""Independent executor ownership; additive while disabled, protocol2 activation.

Serialized after 0063_collector_stage_items, which follows 0062_delivery_change_sets.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0064_run_executor"
down_revision = "0063_collector_stage_items"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runtime_generations", sa.Column("role", sa.String(16), nullable=False, server_default="api"))
    op.add_column("runtime_generations", sa.Column("protocol", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("runtime_generations", sa.Column("execution_mode", sa.String(16), nullable=False, server_default="local"))
    op.add_column("runtime_control", sa.Column("executor_generation", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_runtime_control_executor", "runtime_control", "runtime_generations",
                          ["executor_generation"], ["id"])
    op.create_table("runtime_process_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("generation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runtime_generations.id"), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("process_started_at", sa.Float(), nullable=False),
        sa.Column("pgid", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "lease_token", "attempt", name="uq_runtime_process_groups_fence_attempt"))
    op.create_index("ix_runtime_process_groups_run_id", "runtime_process_groups", ["run_id"])
    op.create_index("ix_runtime_process_groups_generation_id", "runtime_process_groups", ["generation_id"])
    install_guards()


def install_guards():
    """Also used against ORM-created disposable schemas by integration tests."""
    op.execute("""
    CREATE FUNCTION guard_executor_control() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.executor_generation IS DISTINCT FROM OLD.executor_generation THEN
        IF EXISTS (SELECT 1 FROM runtime_leases) THEN
          RAISE EXCEPTION 'executor transition blocked by existing lease';
        END IF;
        IF OLD.executor_generation IS NOT NULL AND EXISTS (
          SELECT 1 FROM runtime_commands WHERE generation_id=OLD.executor_generation AND acknowledged_at IS NULL
        ) THEN RAISE EXCEPTION 'executor transition blocked by pending commands'; END IF;
        IF NEW.executor_generation IS NOT NULL THEN
          IF NOT EXISTS (SELECT 1 FROM runtime_generations WHERE id=NEW.executor_generation
              AND role='executor' AND protocol=2 AND state='active') THEN
            RAISE EXCEPTION 'executor protocol2 active registration required';
          END IF;
          IF EXISTS (SELECT 1 FROM runtime_commands WHERE acknowledged_at IS NULL
              AND generation_id IS DISTINCT FROM NEW.executor_generation) THEN
            RAISE EXCEPTION 'pending commands must be handed to executor atomically';
          END IF;
          IF NOT EXISTS (SELECT 1 FROM runtime_generations WHERE role='api' AND state='active')
            OR EXISTS (SELECT 1 FROM runtime_generations WHERE role='api'
              AND state IN ('active','standby','draining') AND (protocol<>2 OR execution_mode<>'remote')) THEN
            RAISE EXCEPTION 'all APIs must support protocol2 remote mode';
          END IF;
        END IF;
      END IF;
      IF NEW.executor_generation IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM runtime_generations WHERE id=NEW.active_generation AND role='api'
          AND protocol=2 AND execution_mode='remote'
      ) THEN RAISE EXCEPTION 'remote executor refuses incompatible API rollback'; END IF;
      RETURN NEW;
    END $$
    """)
    op.execute("""CREATE TRIGGER executor_control BEFORE UPDATE ON runtime_control
                  FOR EACH ROW EXECUTE FUNCTION guard_executor_control()""")
    op.execute("""
    CREATE FUNCTION guard_executor_api() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE executor uuid;
    BEGIN
      SELECT executor_generation INTO executor FROM runtime_control WHERE id=1 FOR UPDATE;
      IF executor IS NOT NULL AND NEW.role='api'
        AND (NEW.protocol<>2 OR NEW.execution_mode<>'remote') THEN
        RAISE EXCEPTION 'remote executor refuses incompatible API registration';
      END IF;
      RETURN NEW;
    END $$
    """)
    op.execute("""CREATE TRIGGER executor_api BEFORE INSERT OR UPDATE OF role, protocol, execution_mode
                  ON runtime_generations FOR EACH ROW EXECUTE FUNCTION guard_executor_api()""")
    op.execute("""
    CREATE FUNCTION guard_executor_lease() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE executor uuid;
    BEGIN
      SELECT executor_generation INTO executor FROM runtime_control WHERE id=1 FOR UPDATE;
      IF executor IS NOT NULL AND NEW.generation_id<>executor THEN
        RAISE EXCEPTION 'API cannot claim a remote executor Run';
      END IF;
      IF executor IS NULL AND EXISTS (SELECT 1 FROM runtime_generations
          WHERE id=NEW.generation_id AND role='executor') THEN
        RAISE EXCEPTION 'executor is not activated';
      END IF;
      RETURN NEW;
    END $$
    """)
    op.execute("""CREATE TRIGGER executor_lease BEFORE INSERT OR UPDATE ON runtime_leases
                  FOR EACH ROW EXECUTE FUNCTION guard_executor_lease()""")
    op.execute("""
    CREATE FUNCTION route_executor_command() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE executor uuid; owner uuid;
    BEGIN
      SELECT executor_generation INTO executor FROM runtime_control WHERE id=1 FOR UPDATE;
      IF executor IS NOT NULL THEN
        IF NEW.run_id IS NOT NULL THEN
          SELECT generation_id INTO owner FROM runtime_leases WHERE run_id=NEW.run_id;
        ELSIF NEW.session_id IS NOT NULL THEN
          SELECT generation_id INTO owner FROM runtime_leases WHERE session_id=NEW.session_id LIMIT 1;
        END IF;
        NEW.generation_id := coalesce(owner, executor);
      END IF;
      RETURN NEW;
    END $$
    """)
    op.execute("""CREATE TRIGGER executor_command BEFORE INSERT ON runtime_commands
                  FOR EACH ROW EXECUTE FUNCTION route_executor_command()""")


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM runtime_control WHERE executor_generation IS NOT NULL)
        OR EXISTS (SELECT 1 FROM runtime_leases l JOIN runtime_generations g ON g.id=l.generation_id
                   WHERE g.role='executor')
        OR EXISTS (SELECT 1 FROM runtime_process_groups)
        OR EXISTS (SELECT 1 FROM runtime_commands c JOIN runtime_generations g ON g.id=c.generation_id
                   WHERE g.role='executor' AND c.acknowledged_at IS NULL)
      THEN RAISE EXCEPTION 'drain executor and disable remote ownership before downgrade'; END IF;
    END $$""")
    op.drop_index("ix_runtime_process_groups_generation_id", table_name="runtime_process_groups")
    op.drop_index("ix_runtime_process_groups_run_id", table_name="runtime_process_groups")
    op.drop_table("runtime_process_groups")
    for table, name in (("runtime_commands", "command"), ("runtime_leases", "lease"),
                        ("runtime_generations", "api"), ("runtime_control", "control")):
        op.execute(f"DROP TRIGGER executor_{name} ON {table}")
    for function in ("route_executor_command", "guard_executor_lease", "guard_executor_api", "guard_executor_control"):
        op.execute(f"DROP FUNCTION {function}()")
    op.drop_constraint("fk_runtime_control_executor", "runtime_control", type_="foreignkey")
    op.drop_column("runtime_control", "executor_generation")
    for column in ("execution_mode", "protocol", "role"):
        op.drop_column("runtime_generations", column)
