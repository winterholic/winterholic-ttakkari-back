"""Add immutable knowledge provenance and versioned assertion history.

Generated DDL is checked in as a static contract; existing context tables stay intact.
"""

import sqlalchemy as sa

from alembic import op

revision = "0034_knowledge_ledger"
down_revision = "0033_weather_analysis_engine"
branch_labels = None
depends_on = None

DDL = [
    "\nCREATE TABLE knowledge_assertions (\n\tid UUID NOT NULL, \n\tfingerprint VARCHAR(64) NOT NULL, \n\tsubject VARCHAR(200) NOT NULL, \n\tpredicate VARCHAR(100) NOT NULL, \n\tobject TEXT NOT NULL, \n\tsubject_entity_id UUID, \n\tobject_entity_id UUID, \n\tscope VARCHAR(20) NOT NULL, \n\tmodality VARCHAR(20) NOT NULL, \n\tpolarity BOOLEAN NOT NULL, \n\tstate VARCHAR(20) NOT NULL, \n\treview_level VARCHAR(20) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tvalid_from TIMESTAMP WITH TIME ZONE, \n\tvalid_to TIMESTAMP WITH TIME ZONE, \n\tsupersedes_id UUID, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_knowledge_assertion_fingerprint UNIQUE (fingerprint), \n\tCONSTRAINT ck_knowledge_assertion_scope CHECK (scope in ('personal','work','shared')), \n\tCONSTRAINT ck_knowledge_assertion_state CHECK (state in ('candidate','active','disputed','superseded','retracted')), \n\tCONSTRAINT ck_knowledge_assertion_review CHECK (review_level in ('candidate','source_reviewed','user_confirmed')), \n\tCONSTRAINT ck_knowledge_assertion_modality CHECK (modality in ('fact','proposal','question','inference','decision','preference')), \n\tCONSTRAINT ck_knowledge_assertion_time CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to), \n\tCONSTRAINT ck_knowledge_assertion_version CHECK (version > 0), \n\tFOREIGN KEY(subject_entity_id) REFERENCES ctx_entities (id) ON DELETE SET NULL, \n\tFOREIGN KEY(object_entity_id) REFERENCES ctx_entities (id) ON DELETE SET NULL, \n\tFOREIGN KEY(supersedes_id) REFERENCES knowledge_assertions (id)\n)\n\n",
    "\nCREATE TABLE knowledge_review_events (\n\tid UUID NOT NULL, \n\tassertion_id UUID NOT NULL, \n\tversion INTEGER NOT NULL, \n\tactor VARCHAR(20) NOT NULL, \n\tstate VARCHAR(20) NOT NULL, \n\treview_level VARCHAR(20) NOT NULL, \n\treason TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_knowledge_review_version UNIQUE (assertion_id, version), \n\tFOREIGN KEY(assertion_id) REFERENCES knowledge_assertions (id) ON DELETE CASCADE\n)\n\n",
    "\nCREATE TABLE knowledge_revisions (\n\tid UUID NOT NULL, \n\tsource_key VARCHAR(500) NOT NULL, \n\tsha256 VARCHAR(64) NOT NULL, \n\tparser_version VARCHAR(60) NOT NULL, \n\tsource_id UUID, \n\torigin VARCHAR(40) NOT NULL, \n\tscope VARCHAR(20) NOT NULL, \n\tsensitivity VARCHAR(20) NOT NULL, \n\tlocator TEXT, \n\tactive BOOLEAN NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_knowledge_revision_input UNIQUE (source_key, sha256, parser_version), \n\tCONSTRAINT ck_knowledge_revision_scope CHECK (scope in ('personal','work','shared')), \n\tFOREIGN KEY(source_id) REFERENCES ctx_sources (id) ON DELETE SET NULL\n)\n\n",
    "\nCREATE TABLE knowledge_extraction_jobs (\n\tid UUID NOT NULL, \n\trevision_id UUID NOT NULL, \n\textractor_version VARCHAR(100) NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tcursor INTEGER NOT NULL, \n\trun_id UUID, \n\tmanifest JSONB NOT NULL, \n\tcounts JSONB NOT NULL, \n\terror VARCHAR(100), \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_knowledge_extraction_input UNIQUE (revision_id, extractor_version), \n\tCONSTRAINT ck_knowledge_extraction_status CHECK (status in ('queued','running','succeeded','partial','failed','cancelled')), \n\tFOREIGN KEY(revision_id) REFERENCES knowledge_revisions (id) ON DELETE CASCADE, \n\tFOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE SET NULL\n)\n\n",
    "\nCREATE TABLE knowledge_units (\n\tid UUID NOT NULL, \n\trevision_id UUID NOT NULL, \n\texternal_key VARCHAR(500) NOT NULL, \n\tstable_key VARCHAR(64) NOT NULL, \n\tlocator TEXT NOT NULL, \n\tbody TEXT NOT NULL, \n\tsha256 VARCHAR(64) NOT NULL, \n\tsource_offset INTEGER NOT NULL, \n\tspeaker VARCHAR(30) NOT NULL, \n\tactive_branch BOOLEAN, \n\tevent_time TIMESTAMP WITH TIME ZONE, \n\toutcome VARCHAR(30) NOT NULL, \n\treason VARCHAR(100), \n\tprovenance JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_knowledge_unit_occurrence UNIQUE (revision_id, external_key), \n\tCONSTRAINT ck_knowledge_unit_outcome CHECK (outcome in ('pending_extraction','no_message','unsupported_content','invalid')), \n\tCONSTRAINT ck_knowledge_unit_offset CHECK (source_offset >= 0), \n\tFOREIGN KEY(revision_id) REFERENCES knowledge_revisions (id) ON DELETE CASCADE\n)\n\n",
    '\nCREATE TABLE knowledge_evidence (\n\tid UUID NOT NULL, \n\tunit_id UUID NOT NULL, \n\tstart INTEGER NOT NULL, \n\t"end" INTEGER NOT NULL, \n\tsha256 VARCHAR(64) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_knowledge_evidence_span UNIQUE (unit_id, start, "end"), \n\tCONSTRAINT ck_knowledge_evidence_span CHECK (start >= 0 AND "end" > start), \n\tFOREIGN KEY(unit_id) REFERENCES knowledge_units (id) ON DELETE CASCADE\n)\n\n',
    "\nCREATE TABLE knowledge_assertion_evidence (\n\tassertion_id UUID NOT NULL, \n\tevidence_id UUID NOT NULL, \n\tstance VARCHAR(10) NOT NULL, \n\tPRIMARY KEY (assertion_id, evidence_id), \n\tCONSTRAINT ck_knowledge_evidence_stance CHECK (stance in ('supports','refutes')), \n\tFOREIGN KEY(assertion_id) REFERENCES knowledge_assertions (id) ON DELETE CASCADE, \n\tFOREIGN KEY(evidence_id) REFERENCES knowledge_evidence (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_knowledge_assertions_object_entity_id ON knowledge_assertions (object_entity_id)",
    "CREATE INDEX ix_knowledge_assertions_predicate ON knowledge_assertions (predicate)",
    "CREATE INDEX ix_knowledge_assertions_state ON knowledge_assertions (state)",
    "CREATE INDEX ix_knowledge_assertions_subject ON knowledge_assertions (subject)",
    "CREATE INDEX ix_knowledge_assertions_subject_entity_id ON knowledge_assertions (subject_entity_id)",
    "CREATE INDEX ix_knowledge_assertions_supersedes_id ON knowledge_assertions (supersedes_id)",
    "CREATE INDEX ix_knowledge_review_events_assertion_id ON knowledge_review_events (assertion_id)",
    "CREATE INDEX ix_knowledge_revisions_source_id ON knowledge_revisions (source_id)",
    "CREATE INDEX ix_knowledge_revisions_source_key ON knowledge_revisions (source_key)",
    "CREATE UNIQUE INDEX uq_knowledge_revision_active ON knowledge_revisions (source_key) WHERE active",
    "CREATE INDEX ix_knowledge_extraction_jobs_revision_id ON knowledge_extraction_jobs (revision_id)",
    "CREATE INDEX ix_knowledge_extraction_jobs_status ON knowledge_extraction_jobs (status)",
    "CREATE INDEX ix_knowledge_units_revision_id ON knowledge_units (revision_id)",
    "CREATE INDEX ix_knowledge_units_stable_key ON knowledge_units (stable_key)",
    "CREATE INDEX ix_knowledge_evidence_unit_id ON knowledge_evidence (unit_id)",
    "CREATE INDEX ix_knowledge_assertion_evidence_evidence_id ON knowledge_assertion_evidence (evidence_id)",
    "\nCREATE OR REPLACE FUNCTION knowledge_immutable_row() RETURNS trigger AS $$\nBEGIN\n  RAISE EXCEPTION 'knowledge ledger rows are immutable' USING ERRCODE='23514';\nEND;\n$$ LANGUAGE plpgsql\n",
    "\nCREATE OR REPLACE FUNCTION knowledge_validate_span() RETURNS trigger AS $$\nDECLARE original text;\nBEGIN\n  SELECT body INTO original FROM knowledge_units WHERE id=NEW.unit_id;\n  IF original IS NULL OR NEW.start < 0 OR NEW.\"end\" <= NEW.start\n      OR NEW.\"end\" > char_length(original)\n      OR encode(sha256(convert_to(substring(original FROM NEW.start+1 FOR NEW.\"end\"-NEW.start), 'UTF8')), 'hex') <> NEW.sha256 THEN\n    RAISE EXCEPTION 'knowledge evidence span or digest mismatch' USING ERRCODE='23514';\n  END IF;\n  RETURN NEW;\nEND;\n$$ LANGUAGE plpgsql\n",
    "CREATE TRIGGER knowledge_units_immutable BEFORE UPDATE ON knowledge_units FOR EACH ROW EXECUTE FUNCTION knowledge_immutable_row()",
    "CREATE TRIGGER knowledge_evidence_immutable BEFORE UPDATE ON knowledge_evidence FOR EACH ROW EXECUTE FUNCTION knowledge_immutable_row()",
    "CREATE TRIGGER knowledge_review_events_immutable BEFORE UPDATE ON knowledge_review_events FOR EACH ROW EXECUTE FUNCTION knowledge_immutable_row()",
    "CREATE TRIGGER knowledge_evidence_validate BEFORE INSERT ON knowledge_evidence FOR EACH ROW EXECUTE FUNCTION knowledge_validate_span()",
]


def upgrade():
    for statement in DDL:
        op.execute(statement)


def downgrade():
    # Application rollback must retain imported originals and all correction tombstones.
    populated = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM knowledge_revisions) OR EXISTS(SELECT 1 FROM knowledge_assertions)"
        )
    )
    if populated:
        raise RuntimeError(
            "knowledge_ledger_contains_data: rollback application code; preserve ledger schema"
        )
    op.drop_table("knowledge_assertion_evidence")
    op.drop_table("knowledge_evidence")
    op.drop_table("knowledge_units")
    op.drop_table("knowledge_extraction_jobs")
    op.drop_table("knowledge_revisions")
    op.drop_table("knowledge_review_events")
    op.drop_table("knowledge_assertions")
    op.execute("DROP FUNCTION IF EXISTS knowledge_immutable_row()")
    op.execute("DROP FUNCTION IF EXISTS knowledge_validate_span()")
