"""Converge collection missions and Research Lab without rewriting either history.

Each branch owns separate schema additions. Alembic applies the missing branch
before this merge, whether the database starts at the collection or research tip.
"""

revision = "0075_collection_research_merge"
down_revision = ("0074_collection_single_library", "0073_research_reuse_source")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
