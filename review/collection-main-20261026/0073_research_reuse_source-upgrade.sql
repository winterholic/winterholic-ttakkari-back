BEGIN;

-- Running upgrade 0071_library_platform_v2_merge -> 0072_collection_missions

CREATE TABLE collection_missions (
    id UUID NOT NULL, 
    tenant_id VARCHAR(64) NOT NULL, 
    request_key VARCHAR(256) NOT NULL, 
    input_hash VARCHAR(64) NOT NULL, 
    goal TEXT NOT NULL, 
    plan JSONB NOT NULL, 
    status VARCHAR(32) NOT NULL, 
    gaps JSONB NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_collection_mission_request UNIQUE (tenant_id, request_key)
);

CREATE INDEX ix_collection_mission_created ON collection_missions (tenant_id, created_at);

CREATE TABLE collection_mission_jobs (
    mission_id UUID NOT NULL, 
    job_id UUID NOT NULL, 
    PRIMARY KEY (mission_id, job_id), 
    FOREIGN KEY(mission_id) REFERENCES collection_missions (id) ON DELETE RESTRICT, 
    FOREIGN KEY(job_id) REFERENCES collector_jobs (id) ON DELETE RESTRICT
);

INSERT INTO alembic_version (version_num) VALUES ('0072_collection_missions') RETURNING alembic_version.version_num;

-- Running upgrade 0072_collection_missions -> 0073_collection_mission_runs

ALTER TABLE collection_missions ADD COLUMN current_run_seq INTEGER DEFAULT '1' NOT NULL;

ALTER TABLE collection_mission_jobs ADD COLUMN run_seq INTEGER DEFAULT '1' NOT NULL;

CREATE INDEX ix_collection_mission_job_run ON collection_mission_jobs (mission_id, run_seq);

CREATE TABLE collection_mission_runs (
    mission_id UUID NOT NULL, 
    run_seq INTEGER NOT NULL, 
    trigger VARCHAR(16) NOT NULL, 
    page_budget INTEGER NOT NULL, 
    extra_pages INTEGER DEFAULT '0' NOT NULL, 
    started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (mission_id, run_seq), 
    FOREIGN KEY(mission_id) REFERENCES collection_missions (id) ON DELETE RESTRICT
);

INSERT INTO collection_mission_runs (mission_id, run_seq, trigger, page_budget, extra_pages, started_at) SELECT id, 1, 'initial', GREATEST(1, COALESCE((plan->'budget'->>'pages')::int, 1)), 0, created_at FROM collection_missions;

UPDATE alembic_version SET version_num='0073_collection_mission_runs' WHERE alembic_version.version_num = '0072_collection_missions';

-- Running upgrade 0073_collection_mission_runs -> 0074_collection_single_library

ALTER TABLE collector_jobs ADD COLUMN pool VARCHAR(16) DEFAULT 'personal' NOT NULL;

UPDATE collector_jobs SET pool = 'work' WHERE scope = 'work';

ALTER TABLE collector_sources ADD COLUMN pool VARCHAR(16) DEFAULT 'personal' NOT NULL;

UPDATE collector_sources SET pool = 'work' WHERE scope = 'work';

ALTER TABLE kb_document_access ADD COLUMN pool VARCHAR(16) DEFAULT 'personal' NOT NULL;

UPDATE kb_document_access SET pool = 'work' WHERE scope = 'work';

CREATE TABLE kb_document_aliases (
    document_id UUID NOT NULL, 
    canonical_document_id UUID NOT NULL, 
    relation VARCHAR(16) NOT NULL, 
    reason VARCHAR(64) NOT NULL, 
    run_id VARCHAR(64) NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (document_id), 
    CONSTRAINT ck_kb_document_aliases_relation CHECK (relation IN ('covered', 'variant')), 
    CONSTRAINT ck_kb_document_aliases_distinct CHECK (document_id <> canonical_document_id), 
    FOREIGN KEY(document_id) REFERENCES kb_documents (id) ON DELETE RESTRICT, 
    FOREIGN KEY(canonical_document_id) REFERENCES kb_documents (id) ON DELETE RESTRICT
);

CREATE INDEX ix_kb_document_aliases_canonical_document_id ON kb_document_aliases (canonical_document_id);

CREATE TABLE collection_scope_ledger (
    id UUID NOT NULL, 
    run_id VARCHAR(64) NOT NULL, 
    table_name VARCHAR(64) NOT NULL, 
    row_key VARCHAR(128) NOT NULL, 
    action VARCHAR(48) NOT NULL, 
    before JSONB NOT NULL, 
    after JSONB NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id)
);

CREATE INDEX ix_collection_scope_ledger_run_id ON collection_scope_ledger (run_id);

UPDATE alembic_version SET version_num='0074_collection_single_library' WHERE alembic_version.version_num = '0073_collection_mission_runs';

-- Running upgrade 0074_collection_single_library, 0073_research_reuse_source -> 0075_collection_research_merge

DELETE FROM alembic_version WHERE alembic_version.version_num = '0074_collection_single_library';

UPDATE alembic_version SET version_num='0075_collection_research_merge' WHERE alembic_version.version_num = '0073_research_reuse_source';

COMMIT;

