"""Converge the KB v3 and collection research schema branches.

Both heads (0074_kb_v3_research_merge and 0075_collection_research_merge)
were published independently on top of their respective lineages.
Keep their identities intact and converge with a no-op merge revision.

Revision ID: 0076_kb_v3_collection_merge
Revises: 0074_kb_v3_research_merge, 0075_collection_research_merge
Create Date: 2026-10-27
"""

revision = "0076_kb_v3_collection_merge"
down_revision = ("0074_kb_v3_research_merge", "0075_collection_research_merge")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
