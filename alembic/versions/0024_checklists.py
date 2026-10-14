"""Daily checklists and recurring templates.
Revision ID: 0024
Revises: 0023
"""
import sqlalchemy as sa

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("checklist_templates", sa.Column("id", sa.Uuid(), primary_key=True),
                    sa.Column("title", sa.String(300), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.create_table("checklist_items", sa.Column("id", sa.Uuid(), primary_key=True),
                    sa.Column("day", sa.Date(), nullable=False),
                    sa.Column("template_id", sa.Uuid(), sa.ForeignKey("checklist_templates.id")),
                    sa.Column("title", sa.String(300), nullable=False),
                    sa.Column("checked", sa.Boolean(), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("deleted_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("day", "template_id"))
    op.create_index("ix_checklist_items_day", "checklist_items", ["day"])
    op.create_table("checklist_reminders", sa.Column("day", sa.Date(), primary_key=True),
                    sa.Column("slot", sa.String(5), primary_key=True),
                    sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("checklist_reminders")
    op.drop_table("checklist_items")
    op.drop_table("checklist_templates")
