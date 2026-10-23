"""Managed collection, scoped KB access, observations, shadow quality and vector projections. Additive only."""
from alembic import op

revision = "0065_collector_delivery"
down_revision = "0064_run_executor"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("\nCREATE TABLE collector_sources (\n\tid UUID NOT NULL, \n\ttenant_id VARCHAR(64) NOT NULL, \n\tscope VARCHAR(32) NOT NULL, \n\tsource_key VARCHAR(256) NOT NULL, \n\ttitle VARCHAR(500) NOT NULL, \n\trequest JSONB NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tauto_import BOOLEAN NOT NULL, \n\tinterval_minutes INTEGER, \n\tnext_run_at TIMESTAMP WITH TIME ZONE, \n\tlast_job_id UUID, \n\tversion INTEGER NOT NULL, \n\tlast_error VARCHAR(500), \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_collector_sources_identity UNIQUE (tenant_id, scope, source_key), \n\tCONSTRAINT ck_collector_sources_scope CHECK (scope IN ('personal', 'work')), \n\tCONSTRAINT ck_collector_sources_interval CHECK (interval_minutes IS NULL OR interval_minutes >= 15), \n\tFOREIGN KEY(last_job_id) REFERENCES collector_jobs (id) ON DELETE SET NULL\n)\n\n")
    op.execute('CREATE INDEX ix_collector_sources_next_run_at ON collector_sources (next_run_at)')
    op.execute("\nCREATE TABLE kb_document_access (\n\tdocument_id UUID NOT NULL, \n\ttenant_id VARCHAR(64) NOT NULL, \n\tscope VARCHAR(32) NOT NULL, \n\thidden BOOLEAN NOT NULL, \n\tPRIMARY KEY (document_id), \n\tCONSTRAINT ck_kb_document_access_scope CHECK (scope IN ('personal', 'work')), \n\tFOREIGN KEY(document_id) REFERENCES kb_documents (id) ON DELETE RESTRICT\n)\n\n")
    op.execute('\nCREATE TABLE kb_observations (\n\tid UUID NOT NULL, \n\tdocument_id UUID NOT NULL, \n\trevision_id UUID NOT NULL, \n\tenvelope_id UUID NOT NULL, \n\tevent_time TIMESTAMP WITH TIME ZONE NOT NULL, \n\trecorded_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tprovenance JSONB NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_kb_observations_envelope UNIQUE (envelope_id), \n\tFOREIGN KEY(document_id) REFERENCES kb_documents (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(revision_id) REFERENCES kb_revisions (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('CREATE INDEX ix_kb_observations_document_id ON kb_observations (document_id)')
    op.execute('\nCREATE TABLE collector_deliveries (\n\toutbox_id UUID NOT NULL, \n\titem_id UUID NOT NULL, \n\trevision_id UUID, \n\toutcome VARCHAR(32) NOT NULL, \n\tdetail JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (outbox_id), \n\tFOREIGN KEY(outbox_id) REFERENCES collector_outbox (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(item_id) REFERENCES collector_items (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(revision_id) REFERENCES kb_revisions (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('CREATE INDEX ix_collector_deliveries_item_id ON collector_deliveries (item_id)')
    op.execute('\nCREATE TABLE collector_daily_budgets (\n\tday VARCHAR(10) NOT NULL, \n\treserved_pages INTEGER NOT NULL, \n\tPRIMARY KEY (day)\n)\n\n')
    op.execute('\nCREATE TABLE collector_reviews (\n\tid UUID NOT NULL, \n\titem_id UUID NOT NULL, \n\tdecision VARCHAR(16) NOT NULL, \n\treason VARCHAR(2000) NOT NULL, \n\tactor VARCHAR(32) NOT NULL, \n\ttext_sha256 VARCHAR(64) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(item_id) REFERENCES collector_items (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('CREATE INDEX ix_collector_reviews_item_id ON collector_reviews (item_id)')
    op.execute('\nCREATE TABLE collector_quality_models (\n\tid UUID NOT NULL, \n\ttenant_id VARCHAR(64) NOT NULL, \n\tscope VARCHAR(32) NOT NULL, \n\tdigest VARCHAR(64) NOT NULL, \n\tartifact JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n')
    op.execute('\nCREATE TABLE kb_passage_vectors (\n\tpassage_id UUID NOT NULL, \n\tmodel_key VARCHAR(200) NOT NULL, \n\ttext_sha256 VARCHAR(64) NOT NULL, \n\tvector VECTOR(1024) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (passage_id, model_key), \n\tFOREIGN KEY(passage_id) REFERENCES kb_passages (id) ON DELETE RESTRICT\n)\n\n')
    op.execute("CREATE OR REPLACE FUNCTION collector_evidence_immutable() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'collector evidence is immutable' USING ERRCODE='23514'; END; $$ LANGUAGE plpgsql")
    op.execute("CREATE TRIGGER kb_observations_immutable BEFORE UPDATE OR DELETE ON kb_observations FOR EACH ROW EXECUTE FUNCTION collector_evidence_immutable()")
    op.execute("CREATE TRIGGER collector_reviews_immutable BEFORE UPDATE OR DELETE ON collector_reviews FOR EACH ROW EXECUTE FUNCTION collector_evidence_immutable()")
    op.execute("CREATE TRIGGER collector_quality_models_immutable BEFORE UPDATE OR DELETE ON collector_quality_models FOR EACH ROW EXECUTE FUNCTION collector_evidence_immutable()")

def downgrade():
    op.drop_table('kb_passage_vectors')
    op.drop_table('collector_quality_models')
    op.drop_table('collector_reviews')
    op.drop_table('collector_daily_budgets')
    op.drop_table('collector_deliveries')
    op.drop_table('kb_observations')
    op.drop_table('kb_document_access')
    op.drop_table('collector_sources')
    op.execute("DROP FUNCTION IF EXISTS collector_evidence_immutable()")
