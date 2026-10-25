"""Research Lab v2: Multi-dimensional taxonomy, methods, configurations, runs, measurements, analyses, artifacts, and best results with v1 migration."""

from alembic import op

revision = "0072_research_lab_v2"
down_revision = "0071_library_platform_v2_merge"
branch_labels = None
depends_on = None


def upgrade():
    # 1. research_taxonomy_terms
    op.execute(
        """
        CREATE TABLE research_taxonomy_terms (
            id UUID NOT NULL,
            axis VARCHAR(32) NOT NULL,
            code VARCHAR(64) NOT NULL,
            label VARCHAR(120) NOT NULL,
            label_ko VARCHAR(120) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            parent_code VARCHAR(64),
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_system BOOLEAN NOT NULL DEFAULT TRUE,
            PRIMARY KEY (id),
            CONSTRAINT uq_taxonomy_axis_code UNIQUE (axis, code)
        )
        """
    )
    op.execute("CREATE INDEX ix_research_taxonomy_terms_axis ON research_taxonomy_terms (axis)")
    op.execute("CREATE INDEX ix_research_taxonomy_terms_code ON research_taxonomy_terms (code)")

    # 2. research_methods
    op.execute(
        """
        CREATE TABLE research_methods (
            id UUID NOT NULL,
            scope VARCHAR(16) NOT NULL DEFAULT 'system',
            slug VARCHAR(120) NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            supersedes_id UUID,
            origin VARCHAR(32) NOT NULL DEFAULT 'system_seed',
            lifecycle_status VARCHAR(24) NOT NULL DEFAULT 'validated',
            name VARCHAR(200) NOT NULL,
            name_ko VARCHAR(200) NOT NULL,
            summary TEXT NOT NULL,
            principle TEXT NOT NULL DEFAULT '',
            category VARCHAR(64) NOT NULL DEFAULT 'general',
            targets JSONB NOT NULL DEFAULT '[]',
            layers JSONB NOT NULL DEFAULT '[]',
            goals JSONB NOT NULL DEFAULT '[]',
            interventions JSONB NOT NULL DEFAULT '[]',
            research_methods JSONB NOT NULL DEFAULT '[]',
            parameters JSONB NOT NULL DEFAULT '[]',
            compatibility JSONB NOT NULL DEFAULT '{}',
            metrics JSONB NOT NULL DEFAULT '[]',
            expected_effects JSONB NOT NULL DEFAULT '[]',
            side_effects JSONB NOT NULL DEFAULT '[]',
            evidence JSONB NOT NULL DEFAULT '[]',
            evidence_status VARCHAR(24) NOT NULL DEFAULT 'source_reported',
            adapter_hints JSONB NOT NULL DEFAULT '[]',
            tags JSONB NOT NULL DEFAULT '[]',
            source_study_id UUID,
            source_node_id UUID,
            promoted_from_run_id UUID,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (supersedes_id) REFERENCES research_methods (id),
            FOREIGN KEY (source_study_id) REFERENCES research_studies (id),
            FOREIGN KEY (source_node_id) REFERENCES research_nodes (id),
            CONSTRAINT uq_method_scope_slug_version UNIQUE (scope, slug, version)
        )
        """
    )
    op.execute("CREATE INDEX ix_research_methods_scope ON research_methods (scope)")
    op.execute("CREATE INDEX ix_research_methods_slug ON research_methods (slug)")
    op.execute("CREATE INDEX ix_research_methods_category ON research_methods (category)")
    op.execute("CREATE INDEX ix_research_methods_lifecycle_status ON research_methods (lifecycle_status)")
    op.execute("CREATE INDEX ix_research_methods_targets ON research_methods USING gin (targets)")
    op.execute("CREATE INDEX ix_research_methods_layers ON research_methods USING gin (layers)")
    op.execute("CREATE INDEX ix_research_methods_goals ON research_methods USING gin (goals)")
    op.execute("CREATE INDEX ix_research_methods_interventions ON research_methods USING gin (interventions)")
    op.execute("CREATE INDEX ix_research_methods_research_methods ON research_methods USING gin (research_methods)")

    # 3. research_method_relations
    op.execute(
        """
        CREATE TABLE research_method_relations (
            id UUID NOT NULL,
            scope VARCHAR(16) NOT NULL DEFAULT 'system',
            from_method_id UUID NOT NULL,
            to_method_id UUID NOT NULL,
            relation_type VARCHAR(32) NOT NULL,
            conditions JSONB NOT NULL DEFAULT '{}',
            note TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (id),
            FOREIGN KEY (from_method_id) REFERENCES research_methods (id) ON DELETE CASCADE,
            FOREIGN KEY (to_method_id) REFERENCES research_methods (id) ON DELETE CASCADE,
            CONSTRAINT uq_method_relation UNIQUE (from_method_id, to_method_id, relation_type)
        )
        """
    )
    op.execute("CREATE INDEX ix_research_method_relations_from ON research_method_relations (from_method_id)")
    op.execute("CREATE INDEX ix_research_method_relations_to ON research_method_relations (to_method_id)")
    op.execute("CREATE INDEX ix_research_method_relations_type ON research_method_relations (relation_type)")

    # 4. Expand research_studies
    op.execute("ALTER TABLE research_studies ADD COLUMN IF NOT EXISTS primary_target VARCHAR(64)")
    op.execute("ALTER TABLE research_studies ADD COLUMN IF NOT EXISTS best_result_id UUID")

    # 5. Expand research_experiments
    op.execute("ALTER TABLE research_experiments ADD COLUMN IF NOT EXISTS method_id UUID REFERENCES research_methods (id)")
    op.execute("ALTER TABLE research_experiments ADD COLUMN IF NOT EXISTS compared_method_ids JSONB NOT NULL DEFAULT '[]'")
    op.execute("ALTER TABLE research_experiments ADD COLUMN IF NOT EXISTS adapter_id VARCHAR(64) NOT NULL DEFAULT 'retrieval_eval'")
    op.execute("ALTER TABLE research_experiments ADD COLUMN IF NOT EXISTS strategy VARCHAR(32) NOT NULL DEFAULT 'single'")
    op.execute("ALTER TABLE research_experiments ADD COLUMN IF NOT EXISTS search_space JSONB NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE research_experiments ADD COLUMN IF NOT EXISTS fixed_inputs JSONB NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE research_experiments ADD COLUMN IF NOT EXISTS primary_metric VARCHAR(64) NOT NULL DEFAULT 'recall_at_k'")
    op.execute("ALTER TABLE research_experiments ADD COLUMN IF NOT EXISTS secondary_metrics JSONB NOT NULL DEFAULT '[]'")
    op.execute("ALTER TABLE research_experiments ADD COLUMN IF NOT EXISTS baseline_configuration_id UUID")
    op.execute("CREATE INDEX IF NOT EXISTS ix_research_experiments_method_id ON research_experiments (method_id)")

    # 6. research_configurations
    op.execute(
        """
        CREATE TABLE research_configurations (
            id UUID NOT NULL,
            experiment_id UUID NOT NULL,
            label VARCHAR(120) NOT NULL,
            role VARCHAR(24) NOT NULL DEFAULT 'candidate',
            order_index INTEGER NOT NULL DEFAULT 0,
            parameters JSONB NOT NULL DEFAULT '{}',
            config_hash VARCHAR(64) NOT NULL,
            ablation_factor VARCHAR(64),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (experiment_id) REFERENCES research_experiments (id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX ix_research_configurations_experiment ON research_configurations (experiment_id)")
    op.execute("CREATE INDEX ix_research_configurations_hash ON research_configurations (config_hash)")

    # 7. Add index for baseline_configuration_id
    op.execute("CREATE INDEX ix_research_experiments_baseline_cfg ON research_experiments (baseline_configuration_id)")

    # 8. research_runs
    op.execute(
        """
        CREATE TABLE research_runs (
            id UUID NOT NULL,
            experiment_id UUID NOT NULL,
            configuration_id UUID NOT NULL,
            study_id UUID NOT NULL,
            adapter_id VARCHAR(64) NOT NULL,
            execution_origin VARCHAR(24) NOT NULL DEFAULT 'local_measured',
            repeat_index INTEGER NOT NULL DEFAULT 1,
            seed INTEGER NOT NULL DEFAULT 42,
            status VARCHAR(24) NOT NULL DEFAULT 'queued',
            progress FLOAT,
            environment_snapshot JSONB NOT NULL DEFAULT '{}',
            raw_output JSONB NOT NULL DEFAULT '{}',
            error_code VARCHAR(64),
            error_message TEXT,
            started_at TIMESTAMP WITH TIME ZONE,
            finished_at TIMESTAMP WITH TIME ZONE,
            wall_seconds FLOAT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (experiment_id) REFERENCES research_experiments (id) ON DELETE CASCADE,
            FOREIGN KEY (configuration_id) REFERENCES research_configurations (id) ON DELETE CASCADE,
            FOREIGN KEY (study_id) REFERENCES research_studies (id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX ix_research_runs_experiment ON research_runs (experiment_id)")
    op.execute("CREATE INDEX ix_research_runs_config ON research_runs (configuration_id)")
    op.execute("CREATE INDEX ix_research_runs_study ON research_runs (study_id)")
    op.execute("CREATE INDEX ix_research_runs_status ON research_runs (status)")
    op.execute("CREATE INDEX ix_research_runs_origin ON research_runs (execution_origin)")

    # 9. research_measurements (with immutable trigger)
    op.execute(
        """
        CREATE TABLE research_measurements (
            id UUID NOT NULL,
            run_id UUID NOT NULL,
            experiment_id UUID NOT NULL,
            configuration_id UUID NOT NULL,
            metric_key VARCHAR(64) NOT NULL,
            metric_label VARCHAR(120) NOT NULL,
            value DOUBLE PRECISION NOT NULL,
            unit VARCHAR(32) NOT NULL DEFAULT '',
            direction VARCHAR(24) NOT NULL DEFAULT 'higher_is_better',
            is_proxy BOOLEAN NOT NULL DEFAULT FALSE,
            aggregator VARCHAR(24) NOT NULL DEFAULT 'single',
            slice_key VARCHAR(64) NOT NULL DEFAULT 'overall',
            meta JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (run_id) REFERENCES research_runs (id) ON DELETE CASCADE,
            FOREIGN KEY (experiment_id) REFERENCES research_experiments (id) ON DELETE CASCADE,
            FOREIGN KEY (configuration_id) REFERENCES research_configurations (id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX ix_research_measurements_run ON research_measurements (run_id)")
    op.execute("CREATE INDEX ix_research_measurements_exp ON research_measurements (experiment_id)")
    op.execute("CREATE INDEX ix_research_measurements_cfg ON research_measurements (configuration_id)")
    op.execute("CREATE INDEX ix_research_measurements_key ON research_measurements (metric_key)")
    op.execute(
        """
        CREATE TRIGGER research_measurements_immutable
            BEFORE UPDATE OR DELETE ON research_measurements
            FOR EACH ROW EXECUTE FUNCTION research_immutable_row()
        """
    )

    # 10. research_analyses
    op.execute(
        """
        CREATE TABLE research_analyses (
            id UUID NOT NULL,
            experiment_id UUID NOT NULL,
            baseline_configuration_id UUID,
            best_configuration_id UUID,
            primary_metric VARCHAR(64) NOT NULL,
            summary JSONB NOT NULL DEFAULT '{}',
            recommendation_ko TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (experiment_id) REFERENCES research_experiments (id) ON DELETE CASCADE,
            FOREIGN KEY (baseline_configuration_id) REFERENCES research_configurations (id) ON DELETE SET NULL,
            FOREIGN KEY (best_configuration_id) REFERENCES research_configurations (id) ON DELETE SET NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_research_analyses_experiment ON research_analyses (experiment_id)")

    # 11. research_artifacts
    op.execute(
        """
        CREATE TABLE research_artifacts (
            id UUID NOT NULL,
            study_id UUID NOT NULL,
            experiment_id UUID,
            run_id UUID,
            artifact_id UUID NOT NULL,
            role VARCHAR(32) NOT NULL,
            label VARCHAR(200) NOT NULL,
            promoted_to_library BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (study_id) REFERENCES research_studies (id) ON DELETE CASCADE,
            FOREIGN KEY (experiment_id) REFERENCES research_experiments (id) ON DELETE SET NULL,
            FOREIGN KEY (run_id) REFERENCES research_runs (id) ON DELETE SET NULL,
            FOREIGN KEY (artifact_id) REFERENCES artifacts (id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX ix_research_artifacts_study ON research_artifacts (study_id)")
    op.execute("CREATE INDEX ix_research_artifacts_experiment ON research_artifacts (experiment_id)")
    op.execute("CREATE INDEX ix_research_artifacts_run ON research_artifacts (run_id)")

    # 12. research_best_results
    op.execute(
        """
        CREATE TABLE research_best_results (
            id UUID NOT NULL,
            study_id UUID NOT NULL,
            experiment_id UUID NOT NULL,
            configuration_id UUID NOT NULL,
            run_id UUID NOT NULL,
            decision_node_id UUID,
            status VARCHAR(24) NOT NULL DEFAULT 'adopted',
            objective TEXT NOT NULL DEFAULT '',
            primary_metric VARCHAR(64) NOT NULL DEFAULT '',
            constraints JSONB NOT NULL DEFAULT '{}',
            rationale TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (study_id) REFERENCES research_studies (id) ON DELETE CASCADE,
            FOREIGN KEY (experiment_id) REFERENCES research_experiments (id) ON DELETE CASCADE,
            FOREIGN KEY (configuration_id) REFERENCES research_configurations (id) ON DELETE CASCADE,
            FOREIGN KEY (run_id) REFERENCES research_runs (id) ON DELETE CASCADE,
            FOREIGN KEY (decision_node_id) REFERENCES research_nodes (id) ON DELETE SET NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_research_best_results_study ON research_best_results (study_id)")
    op.execute("CREATE INDEX ix_research_best_results_exp ON research_best_results (experiment_id)")
    op.execute("CREATE INDEX ix_research_studies_best_result_id ON research_studies (best_result_id)")

    # 13. Data Migration for existing v1 research_experiments
    op.execute(
        """
        DO $$
        DECLARE
            exp_row RECORD;
            new_cfg_id UUID;
            new_run_id UUID;
            m_key TEXT;
            m_val NUMERIC;
            means_obj JSONB;
        BEGIN
            FOR exp_row IN SELECT * FROM research_experiments LOOP
                new_cfg_id := gen_random_uuid();
                new_run_id := gen_random_uuid();

                -- Create baseline Configuration
                INSERT INTO research_configurations (
                    id, experiment_id, label, role, order_index, parameters, config_hash, created_at
                ) VALUES (
                    new_cfg_id,
                    exp_row.id,
                    'Migrated v1 Config',
                    'baseline',
                    0,
                    COALESCE(exp_row.config, '{}'::jsonb),
                    md5(COALESCE(exp_row.config, '{}'::jsonb)::text),
                    COALESCE(exp_row.created_at, NOW())
                );

                -- Link baseline_configuration_id back to experiment
                UPDATE research_experiments
                SET baseline_configuration_id = new_cfg_id,
                    adapter_id = CASE WHEN exp_row.adapter IS NOT NULL AND exp_row.adapter <> '' THEN exp_row.adapter ELSE 'retrieval_eval' END
                WHERE id = exp_row.id;

                -- Create Run
                INSERT INTO research_runs (
                    id, experiment_id, configuration_id, study_id, adapter_id,
                    execution_origin, repeat_index, seed, status,
                    environment_snapshot, raw_output, started_at, finished_at, created_at
                ) VALUES (
                    new_run_id,
                    exp_row.id,
                    new_cfg_id,
                    exp_row.study_id,
                    CASE WHEN exp_row.adapter IS NOT NULL AND exp_row.adapter <> '' THEN exp_row.adapter ELSE 'retrieval_eval' END,
                    'migrated_v1',
                    1,
                    42,
                    CASE WHEN exp_row.state = 'completed' THEN 'succeeded' ELSE exp_row.state END,
                    COALESCE(exp_row.environment, '{}'::jsonb),
                    COALESCE(exp_row.receipt, '{}'::jsonb),
                    exp_row.created_at,
                    exp_row.finished_at,
                    COALESCE(exp_row.created_at, NOW())
                );

                -- Extract measurements from receipt->'result'->'means' if available
                IF exp_row.receipt ? 'result' AND (exp_row.receipt->'result') ? 'means' THEN
                    means_obj := exp_row.receipt->'result'->'means';
                    IF jsonb_typeof(means_obj) = 'object' THEN
                        FOR m_key, m_val IN SELECT key, value::text::numeric FROM jsonb_each_text(means_obj) LOOP
                            INSERT INTO research_measurements (
                                id, run_id, experiment_id, configuration_id,
                                metric_key, metric_label, value, unit, direction,
                                is_proxy, aggregator, slice_key, meta, created_at
                            ) VALUES (
                                gen_random_uuid(),
                                new_run_id,
                                exp_row.id,
                                new_cfg_id,
                                m_key,
                                m_key,
                                m_val,
                                'ratio',
                                'higher_is_better',
                                FALSE,
                                'single',
                                'overall',
                                '{}'::jsonb,
                                COALESCE(exp_row.finished_at, exp_row.created_at, NOW())
                            );
                        END LOOP;
                    END IF;
                END IF;
            END LOOP;
        END $$;
        """
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_research_studies_best_result_id")
    op.execute("DROP INDEX IF EXISTS ix_research_experiments_baseline_cfg")
    op.execute("ALTER TABLE research_studies DROP CONSTRAINT IF EXISTS fk_study_best_result")
    op.execute("ALTER TABLE research_experiments DROP CONSTRAINT IF EXISTS fk_exp_baseline_cfg")
    op.execute("ALTER TABLE research_experiments DROP CONSTRAINT IF EXISTS research_experiments_method_id_fkey")

    op.execute("ALTER TABLE research_experiments DROP COLUMN IF EXISTS method_id")
    op.execute("ALTER TABLE research_experiments DROP COLUMN IF EXISTS compared_method_ids")
    op.execute("ALTER TABLE research_experiments DROP COLUMN IF EXISTS adapter_id")
    op.execute("ALTER TABLE research_experiments DROP COLUMN IF EXISTS strategy")
    op.execute("ALTER TABLE research_experiments DROP COLUMN IF EXISTS search_space")
    op.execute("ALTER TABLE research_experiments DROP COLUMN IF EXISTS fixed_inputs")
    op.execute("ALTER TABLE research_experiments DROP COLUMN IF EXISTS primary_metric")
    op.execute("ALTER TABLE research_experiments DROP COLUMN IF EXISTS secondary_metrics")
    op.execute("ALTER TABLE research_experiments DROP COLUMN IF EXISTS baseline_configuration_id")

    op.execute("ALTER TABLE research_studies DROP COLUMN IF EXISTS primary_target")
    op.execute("ALTER TABLE research_studies DROP COLUMN IF EXISTS best_result_id")

    op.execute("DROP TABLE IF EXISTS research_best_results CASCADE")
    op.execute("DROP TABLE IF EXISTS research_artifacts CASCADE")
    op.execute("DROP TABLE IF EXISTS research_analyses CASCADE")
    op.execute("DROP TABLE IF EXISTS research_measurements CASCADE")
    op.execute("DROP TABLE IF EXISTS research_runs CASCADE")
    op.execute("DROP TABLE IF EXISTS research_configurations CASCADE")
    op.execute("DROP TABLE IF EXISTS research_method_relations CASCADE")
    op.execute("DROP TABLE IF EXISTS research_methods CASCADE")
    op.execute("DROP TABLE IF EXISTS research_taxonomy_terms CASCADE")
