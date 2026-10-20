"""Independent Research Lab aggregates; additive, history-preserving schema."""

from alembic import op

revision = "0053_research_lab"
down_revision = "0052_artifact_area_byproducts"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "\nCREATE TABLE research_studies (\n\tid UUID NOT NULL, \n\ttitle VARCHAR(240) NOT NULL, \n\tgoal TEXT NOT NULL, \n\tscope VARCHAR(20) NOT NULL, \n\tphase VARCHAR(100) NOT NULL, \n\tenvironment JSONB NOT NULL, \n\tcriteria JSONB NOT NULL, \n\tnext_actions JSONB NOT NULL, \n\tversion INTEGER NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_research_scope CHECK (scope in ('personal','work'))\n)\n\n"
    )
    op.execute("CREATE INDEX ix_research_studies_scope ON research_studies (scope)")
    op.execute(
        "\nCREATE TABLE research_nodes (\n\tid UUID NOT NULL, \n\tstudy_id UUID NOT NULL, \n\tkind VARCHAR(100) NOT NULL, \n\ttitle VARCHAR(240) NOT NULL, \n\tsummary TEXT NOT NULL, \n\tconditions JSONB NOT NULL, \n\tdetails JSONB NOT NULL, \n\tepistemic_status VARCHAR(40) NOT NULL, \n\tevidence_id UUID, \n\tsupersedes_id UUID, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(study_id) REFERENCES research_studies (id), \n\tFOREIGN KEY(evidence_id) REFERENCES knowledge_evidence (id), \n\tFOREIGN KEY(supersedes_id) REFERENCES research_nodes (id)\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_research_nodes_evidence_id ON research_nodes (evidence_id)"
    )
    op.execute("CREATE INDEX ix_research_nodes_kind ON research_nodes (kind)")
    op.execute("CREATE INDEX ix_research_nodes_study_id ON research_nodes (study_id)")
    op.execute(
        "\nCREATE TABLE research_relations (\n\tid UUID NOT NULL, \n\tstudy_id UUID NOT NULL, \n\tfrom_id UUID NOT NULL, \n\tto_id UUID NOT NULL, \n\tpredicate VARCHAR(100) NOT NULL, \n\trationale TEXT NOT NULL, \n\tconditions JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(study_id) REFERENCES research_studies (id), \n\tFOREIGN KEY(from_id) REFERENCES research_nodes (id), \n\tFOREIGN KEY(to_id) REFERENCES research_nodes (id)\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_research_relations_from_id ON research_relations (from_id)"
    )
    op.execute(
        "CREATE INDEX ix_research_relations_study_id ON research_relations (study_id)"
    )
    op.execute("CREATE INDEX ix_research_relations_to_id ON research_relations (to_id)")
    op.execute(
        "\nCREATE TABLE research_experiments (\n\tid UUID NOT NULL, \n\tstudy_id UUID NOT NULL, \n\ttitle VARCHAR(240) NOT NULL, \n\thypothesis TEXT NOT NULL, \n\trationale TEXT NOT NULL, \n\tadapter VARCHAR(100) NOT NULL, \n\tstate VARCHAR(30) NOT NULL, \n\tconfig JSONB NOT NULL, \n\tenvironment JSONB NOT NULL, \n\tbudget JSONB NOT NULL, \n\treceipt JSONB NOT NULL, \n\tmethod_ids JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_research_execution_state CHECK (state in ('queued','running','paused','blocked','completed','failed','cancelled','needs_verification')), \n\tFOREIGN KEY(study_id) REFERENCES research_studies (id)\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_research_experiments_state ON research_experiments (state)"
    )
    op.execute(
        "CREATE INDEX ix_research_experiments_study_id ON research_experiments (study_id)"
    )
    op.execute(
        "\nCREATE TABLE research_events (\n\tid UUID NOT NULL, \n\tstudy_id UUID NOT NULL, \n\tkind VARCHAR(100) NOT NULL, \n\tactor VARCHAR(100) NOT NULL, \n\tpayload JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(study_id) REFERENCES research_studies (id)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_research_events_study_id ON research_events (study_id)")
    op.execute(
        "\nCREATE OR REPLACE FUNCTION research_immutable_row() RETURNS trigger AS $$\nBEGIN\n  RAISE EXCEPTION 'research history is immutable; append a correction' USING ERRCODE='23514';\nEND;\n$$ LANGUAGE plpgsql\n"
    )
    op.execute(
        "\nCREATE OR REPLACE FUNCTION research_validate_relation() RETURNS trigger AS $$\nBEGIN\n  IF NOT EXISTS (SELECT 1 FROM research_nodes WHERE id=NEW.from_id AND study_id=NEW.study_id)\n    OR NOT EXISTS (SELECT 1 FROM research_nodes WHERE id=NEW.to_id AND study_id=NEW.study_id) THEN\n    RAISE EXCEPTION 'research relation study mismatch' USING ERRCODE='23514';\n  END IF;\n  RETURN NEW;\nEND;\n$$ LANGUAGE plpgsql\n"
    )
    op.execute(
        "\nCREATE OR REPLACE FUNCTION research_validate_node() RETURNS trigger AS $$\nBEGIN\n  IF NEW.kind IN ('claim','finding') AND NEW.evidence_id IS NULL THEN\n    RAISE EXCEPTION 'source evidence required' USING ERRCODE='23514';\n  END IF;\n  IF NEW.evidence_id IS NOT NULL AND NOT EXISTS (\n    SELECT 1 FROM knowledge_evidence e JOIN knowledge_units u ON u.id=e.unit_id\n    JOIN knowledge_revisions r ON r.id=u.revision_id JOIN research_studies s ON s.id=NEW.study_id\n    WHERE e.id=NEW.evidence_id AND r.scope=s.scope) THEN\n    RAISE EXCEPTION 'research evidence scope mismatch' USING ERRCODE='23514';\n  END IF;\n  IF NEW.supersedes_id IS NOT NULL AND NOT EXISTS (\n    SELECT 1 FROM research_nodes WHERE id=NEW.supersedes_id AND study_id=NEW.study_id) THEN\n    RAISE EXCEPTION 'research correction study mismatch' USING ERRCODE='23514';\n  END IF;\n  RETURN NEW;\nEND;\n$$ LANGUAGE plpgsql\n"
    )
    op.execute(
        "CREATE TRIGGER research_nodes_immutable BEFORE UPDATE OR DELETE ON research_nodes FOR EACH ROW EXECUTE FUNCTION research_immutable_row()"
    )
    op.execute(
        "CREATE TRIGGER research_relations_immutable BEFORE UPDATE OR DELETE ON research_relations FOR EACH ROW EXECUTE FUNCTION research_immutable_row()"
    )
    op.execute(
        "CREATE TRIGGER research_events_immutable BEFORE UPDATE OR DELETE ON research_events FOR EACH ROW EXECUTE FUNCTION research_immutable_row()"
    )
    op.execute(
        "CREATE TRIGGER research_relation_validate BEFORE INSERT ON research_relations FOR EACH ROW EXECUTE FUNCTION research_validate_relation()"
    )
    op.execute(
        "CREATE TRIGGER research_node_validate BEFORE INSERT ON research_nodes FOR EACH ROW EXECUTE FUNCTION research_validate_node()"
    )


def downgrade():
    op.drop_table("research_events")
    op.drop_table("research_experiments")
    op.drop_table("research_relations")
    op.drop_table("research_nodes")
    op.drop_table("research_studies")
    op.execute("DROP FUNCTION IF EXISTS research_immutable_row()")
    op.execute("DROP FUNCTION IF EXISTS research_validate_relation()")
    op.execute("DROP FUNCTION IF EXISTS research_validate_node()")
