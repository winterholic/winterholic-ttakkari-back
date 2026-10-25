"""Add source_* typed lineage columns to research_experiments.

Revision ID: 0073_research_reuse_source
Revises: 0072_research_lab_v2
Create Date: 2026-10-25
"""

from alembic import op

revision = "0073_research_reuse_source"
down_revision = "0072_research_lab_v2"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add columns if not exist
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'research_experiments' AND column_name = 'source_study_id') THEN
                ALTER TABLE research_experiments ADD COLUMN source_study_id UUID;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'research_experiments' AND column_name = 'source_experiment_id') THEN
                ALTER TABLE research_experiments ADD COLUMN source_experiment_id UUID;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'research_experiments' AND column_name = 'source_configuration_id') THEN
                ALTER TABLE research_experiments ADD COLUMN source_configuration_id UUID;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'research_experiments' AND column_name = 'source_run_id') THEN
                ALTER TABLE research_experiments ADD COLUMN source_run_id UUID;
            END IF;
        END $$;
        """
    )

    # 2. Add foreign key constraints if not exist
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name = 'fk_research_experiments_source_study') THEN
                ALTER TABLE research_experiments ADD CONSTRAINT fk_research_experiments_source_study
                    FOREIGN KEY (source_study_id) REFERENCES research_studies(id) ON DELETE SET NULL;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name = 'fk_research_experiments_source_exp') THEN
                ALTER TABLE research_experiments ADD CONSTRAINT fk_research_experiments_source_exp
                    FOREIGN KEY (source_experiment_id) REFERENCES research_experiments(id) ON DELETE SET NULL;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name = 'fk_research_experiments_source_cfg') THEN
                ALTER TABLE research_experiments ADD CONSTRAINT fk_research_experiments_source_cfg
                    FOREIGN KEY (source_configuration_id) REFERENCES research_configurations(id) ON DELETE SET NULL;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints WHERE constraint_name = 'fk_research_experiments_source_run') THEN
                ALTER TABLE research_experiments ADD CONSTRAINT fk_research_experiments_source_run
                    FOREIGN KEY (source_run_id) REFERENCES research_runs(id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )

    # 3. Add indexes
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_research_experiments_source_study ON research_experiments (source_study_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_research_experiments_source_exp ON research_experiments (source_experiment_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_research_experiments_source_cfg ON research_experiments (source_configuration_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_research_experiments_source_run ON research_experiments (source_run_id)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_research_experiments_source_run")
    op.execute("DROP INDEX IF EXISTS ix_research_experiments_source_cfg")
    op.execute("DROP INDEX IF EXISTS ix_research_experiments_source_exp")
    op.execute("DROP INDEX IF EXISTS ix_research_experiments_source_study")

    op.execute("ALTER TABLE research_experiments DROP CONSTRAINT IF EXISTS fk_research_experiments_source_run")
    op.execute("ALTER TABLE research_experiments DROP CONSTRAINT IF EXISTS fk_research_experiments_source_cfg")
    op.execute("ALTER TABLE research_experiments DROP CONSTRAINT IF EXISTS fk_research_experiments_source_exp")
    op.execute("ALTER TABLE research_experiments DROP CONSTRAINT IF EXISTS fk_research_experiments_source_study")

    op.execute(
        """
        ALTER TABLE research_experiments
            DROP COLUMN IF EXISTS source_run_id,
            DROP COLUMN IF EXISTS source_configuration_id,
            DROP COLUMN IF EXISTS source_experiment_id,
            DROP COLUMN IF EXISTS source_study_id;
        """
    )
