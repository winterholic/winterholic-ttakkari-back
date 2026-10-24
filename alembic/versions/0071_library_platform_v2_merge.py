"""Converge the deployed library schema and platform V2 additions.

Both parents were published independently. Keep their identities and converge
with a merge revision; each branch owns separate tables/columns.
"""

revision = "0071_library_platform_v2_merge"
down_revision = ("0068_library_intent", "0070_background_consumer_queue")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
