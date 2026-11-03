"""Cluster-wide leased reservations and per-call L2 budget ledger.

Revision ID: 0073_kb_l2_budget_ledger
Revises: 0072_kb_v3_schema_and_scope
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0073_kb_l2_budget_ledger"
down_revision = "0072_kb_v3_schema_and_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_l2_budget_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("budget_date", sa.Date(), nullable=False),
        sa.Column("reservation_key", sa.String(length=160), nullable=False),
        sa.Column("reserved_calls", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_call_no", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("reservation_key", name="uq_kb_l2_budget_reservation_key"),
        sa.CheckConstraint("reserved_calls > 0", name="ck_kb_l2_budget_reserved_positive"),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'cancelled', 'expired')",
            name="ck_kb_l2_budget_reservation_status",
        ),
    )
    op.create_index(
        "ix_kb_l2_budget_reservations_day_status",
        "kb_l2_budget_reservations",
        ["budget_date", "status"],
    )
    op.create_table(
        "kb_l2_budget_usage",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("call_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["kb_l2_budget_reservations.id"],
            name="fk_kb_l2_budget_usage_reservation", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "reservation_id", "call_no", name="uq_kb_l2_budget_usage_call"
        ),
        sa.CheckConstraint(
            "status IN ('in_flight', 'consumed', 'released')",
            name="ck_kb_l2_budget_usage_status",
        ),
    )
    op.create_index(
        "ix_kb_l2_budget_usage_reservation", "kb_l2_budget_usage", ["reservation_id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    usage = bind.execute(sa.text("SELECT count(*) FROM kb_l2_budget_usage")).scalar_one()
    reservations = bind.execute(sa.text("SELECT count(*) FROM kb_l2_budget_reservations")).scalar_one()
    if usage or reservations:
        raise RuntimeError(
            "refusing to downgrade 0073 while L2 budget ledger data exists; archive it explicitly first"
        )
    op.drop_index("ix_kb_l2_budget_usage_reservation", table_name="kb_l2_budget_usage")
    op.drop_table("kb_l2_budget_usage")
    op.drop_index(
        "ix_kb_l2_budget_reservations_day_status", table_name="kb_l2_budget_reservations"
    )
    op.drop_table("kb_l2_budget_reservations")
