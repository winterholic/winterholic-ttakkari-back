"""Collector absolute spend ledger, scoped result cache and runtime flags. Additive only (CP-S2-05, CP-S2-10)."""
from alembic import op

revision = "0066_collector_budget"
down_revision = "0065_collector_delivery"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("\nCREATE TABLE collector_budget_buckets (\n\tkind VARCHAR(8) NOT NULL, \n\tkey VARCHAR(64) NOT NULL, \n\tlimit_tokens BIGINT NOT NULL, \n\tlimit_calls INTEGER NOT NULL, \n\tlimit_micro_usd BIGINT NOT NULL, \n\treserved_tokens BIGINT NOT NULL, \n\treserved_calls INTEGER NOT NULL, \n\treserved_micro_usd BIGINT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (kind, key), \n\tCONSTRAINT ck_collector_budget_buckets_kind CHECK (kind IN ('day', 'job')), \n\tCONSTRAINT ck_collector_budget_buckets_nonneg CHECK (reserved_tokens >= 0 AND reserved_calls >= 0 AND reserved_micro_usd >= 0)\n)\n")
    op.execute("\nCREATE TABLE collector_budget_reservations (\n\tid UUID NOT NULL, \n\trequest_key VARCHAR(200) NOT NULL, \n\tday VARCHAR(10) NOT NULL, \n\tjob_id VARCHAR(64), \n\tpurpose VARCHAR(24) NOT NULL, \n\tengine VARCHAR(80) NOT NULL, \n\tstate VARCHAR(12) NOT NULL, \n\treserved_tokens BIGINT NOT NULL, \n\treserved_micro_usd BIGINT NOT NULL, \n\tcharged_tokens BIGINT NOT NULL, \n\tcharged_micro_usd BIGINT NOT NULL, \n\treceipt JSONB, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tsettled_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_collector_budget_reservations_key UNIQUE (request_key), \n\tCONSTRAINT ck_collector_budget_reservations_state CHECK (state IN ('reserved', 'settled', 'unknown'))\n)\n")
    op.execute('\nCREATE INDEX ix_collector_budget_reservations_day ON collector_budget_reservations (day)\n')
    op.execute("\nCREATE TABLE collector_cache (\n\tcache_key VARCHAR(64) NOT NULL, \n\ttenant_id VARCHAR(64) NOT NULL, \n\tscope VARCHAR(32) NOT NULL, \n\tkind VARCHAR(10) NOT NULL, \n\tresult JSONB NOT NULL, \n\treason VARCHAR(200), \n\thits INTEGER NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (cache_key), \n\tCONSTRAINT ck_collector_cache_kind CHECK (kind IN ('positive', 'negative', 'unknown'))\n)\n")
    op.execute('\nCREATE INDEX ix_collector_cache_expires_at ON collector_cache (expires_at)\n')
    op.execute('\nCREATE TABLE collector_runtime_flags (\n\tname VARCHAR(64) NOT NULL, \n\tvalue JSONB NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (name)\n)\n')


def downgrade():
    op.drop_table('collector_runtime_flags')
    op.drop_table('collector_cache')
    op.drop_table('collector_budget_reservations')
    op.drop_table('collector_budget_buckets')
