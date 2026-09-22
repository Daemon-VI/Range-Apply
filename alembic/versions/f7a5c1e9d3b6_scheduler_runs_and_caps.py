"""scheduler_runs_and_caps

Blueprint Phase 5: scheduler run records, the concurrency-safe application
cap ledger, attempt reservation fields on ``applications``, the scheduler's
last decision on ``candidate_opportunities`` and two policy fields
(``minimum_fit_score``, ``timezone``).

Revision ID: f7a5c1e9d3b6
Revises: e6f4b0d8c2a5
Create Date: 2026-09-13 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a5c1e9d3b6"
down_revision: Union[str, None] = "e6f4b0d8c2a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduler_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("window_size", sa.Integer(), nullable=False),
        sa.Column("resumed_from_run_id", sa.String(length=36), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("considered", sa.Integer(), nullable=False),
        sa.Column("admissible", sa.Integer(), nullable=False),
        sa.Column("admitted", sa.Integer(), nullable=False),
        sa.Column("enqueued", sa.Integer(), nullable=False),
        sa.Column("requeued", sa.Integer(), nullable=False),
        sa.Column("already_queued", sa.Integer(), nullable=False),
        sa.Column("already_completed", sa.Integer(), nullable=False),
        sa.Column("ready_for_execution", sa.Integer(), nullable=False),
        sa.Column("needs_review", sa.Integer(), nullable=False),
        sa.Column("needs_user_input", sa.Integer(), nullable=False),
        sa.Column("released", sa.Integer(), nullable=False),
        sa.Column("deferred", sa.Integer(), nullable=False),
        sa.Column("blocked", sa.Integer(), nullable=False),
        sa.Column("blocked_by_reason", sa.JSON(), nullable=False),
        sa.Column("samples", sa.JSON(), nullable=False),
        sa.Column("preparation", sa.JSON(), nullable=True),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduler_runs_tenant_started", "scheduler_runs", ["tenant_id", "started_at"])
    op.create_index("ix_scheduler_runs_tenant_status", "scheduler_runs", ["tenant_id", "status"])

    op.create_table(
        "application_cap_ledger",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("period_kind", sa.String(length=8), nullable=False),
        sa.Column("period_key", sa.String(length=16), nullable=False),
        sa.Column("reserved", sa.Integer(), nullable=False),
        sa.Column("released", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "period_kind", "period_key", name="uq_cap_ledger_period"),
    )

    with op.batch_alter_table("application_policies") as batch:
        batch.add_column(sa.Column("minimum_fit_score", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("timezone", sa.String(length=64), nullable=True))

    with op.batch_alter_table("applications") as batch:
        batch.add_column(sa.Column("candidate_opportunity_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("lane", sa.String(length=8), nullable=True))
        batch.add_column(sa.Column("tailoring_level", sa.String(length=4), nullable=True))
        batch.add_column(sa.Column("cap_day", sa.String(length=10), nullable=True))
        batch.add_column(sa.Column("cap_week", sa.String(length=10), nullable=True))
        batch.add_column(sa.Column("reserved_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("released_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key(
            "fk_applications_candidate_opportunity",
            "candidate_opportunities",
            ["candidate_opportunity_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_applications_tenant_cap_day", "applications", ["tenant_id", "cap_day"])
    op.create_index("ix_applications_tenant_cap_week", "applications", ["tenant_id", "cap_week"])
    op.create_index("ix_applications_tenant_status", "applications", ["tenant_id", "status"])

    with op.batch_alter_table("candidate_opportunities") as batch:
        batch.add_column(sa.Column("scheduler_code", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("scheduler_reason", sa.String(length=256), nullable=True))
        batch.add_column(sa.Column("scheduler_run_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("scheduler_decided_at", sa.DateTime(), nullable=True))
    op.create_index("ix_candidate_opportunities_tenant_sched", "candidate_opportunities", ["tenant_id", "scheduler_code"])


def downgrade() -> None:
    op.drop_index("ix_candidate_opportunities_tenant_sched", table_name="candidate_opportunities")
    with op.batch_alter_table("candidate_opportunities") as batch:
        batch.drop_column("scheduler_decided_at")
        batch.drop_column("scheduler_run_id")
        batch.drop_column("scheduler_reason")
        batch.drop_column("scheduler_code")
    op.drop_index("ix_applications_tenant_status", table_name="applications")
    op.drop_index("ix_applications_tenant_cap_week", table_name="applications")
    op.drop_index("ix_applications_tenant_cap_day", table_name="applications")
    with op.batch_alter_table("applications") as batch:
        batch.drop_constraint("fk_applications_candidate_opportunity", type_="foreignkey")
        batch.drop_column("released_at")
        batch.drop_column("reserved_at")
        batch.drop_column("cap_week")
        batch.drop_column("cap_day")
        batch.drop_column("tailoring_level")
        batch.drop_column("lane")
        batch.drop_column("attempt_number")
        batch.drop_column("candidate_opportunity_id")
    with op.batch_alter_table("application_policies") as batch:
        batch.drop_column("timezone")
        batch.drop_column("minimum_fit_score")
    op.drop_table("application_cap_ledger")
    op.drop_index("ix_scheduler_runs_tenant_status", table_name="scheduler_runs")
    op.drop_index("ix_scheduler_runs_tenant_started", table_name="scheduler_runs")
    op.drop_table("scheduler_runs")
