"""Converge the KB v3 and research reuse schema branches.

Both parents were published independently on top of
0071_library_platform_v2_merge. Keep their identities and converge with a
merge revision; each branch owns separate tables/columns (KB v3 vs
research_experiments lineage), so the merge itself is a no-op.

Revision ID: 0074_kb_v3_research_merge
Revises: 0073_research_reuse_source, 0073_kb_l2_budget_ledger
Create Date: 2026-10-26
"""

revision = "0074_kb_v3_research_merge"
down_revision = ("0073_research_reuse_source", "0073_kb_l2_budget_ledger")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
