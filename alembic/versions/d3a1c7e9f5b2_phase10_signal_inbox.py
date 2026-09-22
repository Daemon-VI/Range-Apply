"""phase10_signal_inbox

Blueprint Phase 10: the Signal Inbox (``signals`` + ``signal_observations``),
the append-only attribution history (``signal_attributions``), the
append-only outcome history (``outcome_events``) and the derived current
status per application (``application_outcomes``).

Revision ID: d3a1c7e9f5b2
Revises: c2f0b6d4e8a1
Create Date: 2026-09-17 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3a1c7e9f5b2"
down_revision: Union[str, None] = "c2f0b6d4e8a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "signals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_reference", sa.String(length=256), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=True),
        sa.Column("sender", sa.String(length=256), nullable=True),
        sa.Column("sender_domain", sa.String(length=128), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("external_at", sa.DateTime(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("classification_source", sa.String(length=16), nullable=True),
        sa.Column("classifier_version", sa.String(length=32), nullable=True),
        sa.Column("classification", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_reason", sa.String(length=256), nullable=True),
        sa.Column("attribution_status", sa.String(length=16), nullable=False),
        sa.Column("attribution_id", sa.String(length=36), nullable=True),
        sa.Column("application_id", sa.String(length=36), nullable=True),
        sa.Column("opportunity_id", sa.String(length=36), nullable=True),
        sa.Column("candidate_opportunity_id", sa.String(length=36), nullable=True),
        sa.Column("execution_run_id", sa.String(length=36), nullable=True),
        sa.Column("merged_into_id", sa.String(length=36), nullable=True),
        sa.Column("ai", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["candidate_opportunity_id"], ["candidate_opportunities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["execution_run_id"], ["execution_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_signals_tenant_dedupe"),
    )
    op.create_index("ix_signals_tenant_status", "signals", ["tenant_id", "status"])
    op.create_index("ix_signals_tenant_application", "signals", ["tenant_id", "application_id"])
    op.create_index("ix_signals_tenant_observed", "signals", ["tenant_id", "observed_at"])
    op.create_index("ix_signals_tenant_source", "signals", ["tenant_id", "source"])
    op.create_index("ix_signals_tenant_category", "signals", ["tenant_id", "category"])
    op.create_index("ix_signals_tenant_reference", "signals", ["tenant_id", "source_reference"])

    op.create_table(
        "signal_observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("signal_id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_reference", sa.String(length=256), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_signal_observations_signal_id", "signal_observations", ["signal_id"])

    op.create_table(
        "signal_attributions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("signal_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=True),
        sa.Column("opportunity_id", sa.String(length=36), nullable=True),
        sa.Column("candidate_opportunity_id", sa.String(length=36), nullable=True),
        sa.Column("rule", sa.String(length=48), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("explanation", sa.String(length=512), nullable=True),
        sa.Column("attribution_version", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("superseded_by_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_signal_attributions_signal_id", "signal_attributions", ["signal_id"])
    op.create_index("ix_signal_attributions_tenant_application", "signal_attributions", ["tenant_id", "application_id"])

    op.create_table(
        "outcome_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("signal_id", sa.String(length=36), nullable=True),
        sa.Column("attribution_id", sa.String(length=36), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("evidence", sa.String(length=8), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column("event_at", sa.DateTime(), nullable=False),
        sa.Column("time_basis", sa.String(length=8), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("note", sa.String(length=512), nullable=True),
        sa.Column("classifier_version", sa.String(length=32), nullable=True),
        sa.Column("attribution_version", sa.String(length=32), nullable=True),
        sa.Column("rules_version", sa.String(length=32), nullable=False),
        sa.Column("superseded_by_id", sa.String(length=36), nullable=True),
        sa.Column("retracted", sa.Boolean(), nullable=False),
        sa.Column("retracted_reason", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_outcome_events_tenant_dedupe"),
    )
    op.create_index("ix_outcome_events_application_id", "outcome_events", ["application_id"])
    op.create_index("ix_outcome_events_signal_id", "outcome_events", ["signal_id"])
    op.create_index("ix_outcome_events_tenant_application", "outcome_events", ["tenant_id", "application_id", "sequence"])

    op.create_table(
        "application_outcomes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("opportunity_id", sa.String(length=36), nullable=True),
        sa.Column("candidate_opportunity_id", sa.String(length=36), nullable=True),
        sa.Column("current_outcome", sa.String(length=32), nullable=False),
        sa.Column("provisional_outcome", sa.String(length=32), nullable=True),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.Column("conflicts", sa.JSON(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("basis_event_id", sa.String(length=36), nullable=True),
        sa.Column("last_event_at", sa.DateTime(), nullable=True),
        sa.Column("derived_at", sa.DateTime(), nullable=False),
        sa.Column("rules_version", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "application_id", name="uq_application_outcomes_tenant_application"),
    )
    op.create_index("ix_application_outcomes_tenant_current", "application_outcomes", ["tenant_id", "current_outcome"])
    op.create_index("ix_application_outcomes_tenant_review", "application_outcomes", ["tenant_id", "needs_review"])


def downgrade() -> None:
    op.drop_index("ix_application_outcomes_tenant_review", table_name="application_outcomes")
    op.drop_index("ix_application_outcomes_tenant_current", table_name="application_outcomes")
    op.drop_table("application_outcomes")
    op.drop_index("ix_outcome_events_tenant_application", table_name="outcome_events")
    op.drop_index("ix_outcome_events_signal_id", table_name="outcome_events")
    op.drop_index("ix_outcome_events_application_id", table_name="outcome_events")
    op.drop_table("outcome_events")
    op.drop_index("ix_signal_attributions_tenant_application", table_name="signal_attributions")
    op.drop_index("ix_signal_attributions_signal_id", table_name="signal_attributions")
    op.drop_table("signal_attributions")
    op.drop_index("ix_signal_observations_signal_id", table_name="signal_observations")
    op.drop_table("signal_observations")
    for name in ("ix_signals_tenant_reference", "ix_signals_tenant_category", "ix_signals_tenant_source", "ix_signals_tenant_observed", "ix_signals_tenant_application", "ix_signals_tenant_status"):
        op.drop_index(name, table_name="signals")
    op.drop_table("signals")
