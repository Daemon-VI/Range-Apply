"""execution_foundation

Blueprint Phase 6: execution runs, form snapshots + fields, execution
fields on ``applications`` (submission mutex, handoff reason, verification),
``execution_enqueued`` on scheduler runs and the tenant attribution on
``match_runs``.

Revision ID: a9c7e3f1b5d8
Revises: f7a5c1e9d3b6
Create Date: 2026-09-14 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op
from app.pipeline.policy import company_key

# revision identifiers, used by Alembic.
revision: str = "a9c7e3f1b5d8"
down_revision: Union[str, None] = "f7a5c1e9d3b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "form_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("opportunity_id", sa.String(length=36), nullable=True),
        sa.Column("executor_kind", sa.String(length=24), nullable=False),
        sa.Column("executor_version", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("field_count", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id", "fingerprint", name="uq_form_snapshot_application_fingerprint"),
    )
    op.create_index("ix_form_snapshots_application_id", "form_snapshots", ["application_id"])

    op.create_table(
        "form_fields",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=True),
        sa.Column("label", sa.String(length=512), nullable=False),
        sa.Column("question_key", sa.String(length=512), nullable=False),
        sa.Column("field_type", sa.String(length=16), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("current_value", sa.String(length=1024), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("selected_values", sa.JSON(), nullable=False),
        sa.Column("artifact_type", sa.String(length=16), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("preparation_answer_id", sa.String(length=36), nullable=True),
        sa.Column("evidence_keys", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["form_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_form_fields_snapshot_id", "form_fields", ["snapshot_id"])
    op.create_index("ix_form_fields_tenant_status", "form_fields", ["tenant_id", "status"])

    op.create_table(
        "execution_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("preparation_id", sa.String(length=36), nullable=True),
        sa.Column("candidate_opportunity_id", sa.String(length=36), nullable=True),
        sa.Column("opportunity_id", sa.String(length=36), nullable=True),
        sa.Column("queue_item_id", sa.String(length=36), nullable=True),
        sa.Column("form_snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("executor_kind", sa.String(length=24), nullable=False),
        sa.Column("executor_version", sa.String(length=64), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=True),
        sa.Column("submit_invoked", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("application_url", sa.String(length=2048), nullable=True),
        sa.Column("external_application_id", sa.String(length=256), nullable=True),
        sa.Column("confirmation_reference", sa.String(length=512), nullable=True),
        sa.Column("verification_status", sa.String(length=16), nullable=False),
        sa.Column("verification_method", sa.String(length=32), nullable=True),
        sa.Column("verification_detail", sa.String(length=512), nullable=True),
        sa.Column("error_class", sa.String(length=24), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("handoff_reason", sa.String(length=32), nullable=True),
        sa.Column("handoff", sa.JSON(), nullable=False),
        sa.Column("preconditions", sa.JSON(), nullable=False),
        sa.Column("diagnostics", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["preparation_id"], ["application_preparations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["candidate_opportunity_id"], ["candidate_opportunities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["queue_item_id"], ["application_queue.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["form_snapshot_id"], ["form_snapshots.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_execution_runs_application_id", "execution_runs", ["application_id"])
    op.create_index("ix_execution_runs_tenant_status", "execution_runs", ["tenant_id", "status"])
    op.create_index("ix_execution_runs_tenant_started", "execution_runs", ["tenant_id", "started_at"])

    with op.batch_alter_table("applications") as batch:
        batch.add_column(sa.Column("status_reason", sa.String(length=256), nullable=True))
        batch.add_column(sa.Column("blocked_reason", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("submission_key", sa.String(length=160), nullable=True))
        batch.add_column(sa.Column("execution_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("last_execution_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("verified_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("external_application_id", sa.String(length=256), nullable=True))
        batch.add_column(sa.Column("result_url", sa.String(length=2048), nullable=True))
        batch.create_unique_constraint("uq_applications_submission_key", ["submission_key"])

    with op.batch_alter_table("scheduler_runs") as batch:
        batch.add_column(sa.Column("execution_enqueued", sa.Integer(), nullable=False, server_default="0"))

    with op.batch_alter_table("match_runs") as batch:
        batch.add_column(sa.Column("tenant_id", sa.String(length=64), nullable=True))
        batch.create_foreign_key("fk_match_runs_tenant", "tenants", ["tenant_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_match_runs_tenant_id", "match_runs", ["tenant_id"])

    # Normalised company identity on opportunities, so execution-time
    # blocklist / cool-down / duplicate lookups are indexed instead of
    # scanning every attempt. Backfilled with the same function the app uses.
    with op.batch_alter_table("opportunities") as batch:
        batch.add_column(sa.Column("company_key", sa.String(length=256), nullable=True))
    op.create_index("ix_opportunities_company_key", "opportunities", ["company_key"])
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, company FROM opportunities")).fetchall()
    for row_id, company in rows:
        bind.execute(sa.text("UPDATE opportunities SET company_key = :key WHERE id = :id"), {"key": company_key(company), "id": row_id})


def downgrade() -> None:
    op.drop_index("ix_opportunities_company_key", table_name="opportunities")
    with op.batch_alter_table("opportunities") as batch:
        batch.drop_column("company_key")
    op.drop_index("ix_match_runs_tenant_id", table_name="match_runs")
    with op.batch_alter_table("match_runs") as batch:
        batch.drop_constraint("fk_match_runs_tenant", type_="foreignkey")
        batch.drop_column("tenant_id")
    with op.batch_alter_table("scheduler_runs") as batch:
        batch.drop_column("execution_enqueued")
    with op.batch_alter_table("applications") as batch:
        batch.drop_constraint("uq_applications_submission_key", type_="unique")
        batch.drop_column("result_url")
        batch.drop_column("external_application_id")
        batch.drop_column("verified_at")
        batch.drop_column("last_execution_id")
        batch.drop_column("execution_count")
        batch.drop_column("submission_key")
        batch.drop_column("blocked_reason")
        batch.drop_column("status_reason")
    op.drop_index("ix_execution_runs_tenant_started", table_name="execution_runs")
    op.drop_index("ix_execution_runs_tenant_status", table_name="execution_runs")
    op.drop_index("ix_execution_runs_application_id", table_name="execution_runs")
    op.drop_table("execution_runs")
    op.drop_index("ix_form_fields_tenant_status", table_name="form_fields")
    op.drop_index("ix_form_fields_snapshot_id", table_name="form_fields")
    op.drop_table("form_fields")
    op.drop_index("ix_form_snapshots_application_id", table_name="form_snapshots")
    op.drop_table("form_snapshots")
