"""phase11_learning

Blueprint Phase 11: per-tenant learning settings on the application policy
(ordering opt-in, window, smoothing) and the versioned learning tables
``learning_snapshots``, ``learning_metrics``, ``learning_recommendations``.

Revision ID: e4b2d8f0a6c3
Revises: d3a1c7e9f5b2
Create Date: 2026-09-18 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4b2d8f0a6c3"
down_revision: Union[str, None] = "d3a1c7e9f5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("application_policies") as batch:
        batch.add_column(sa.Column("learning_settings", sa.JSON(), nullable=True))
    op.create_table(
        "learning_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("learning_version", sa.String(length=32), nullable=False),
        sa.Column("feature_version", sa.String(length=32), nullable=False),
        sa.Column("smoothing_method", sa.String(length=48), nullable=False),
        sa.Column("outcome_rules_version", sa.String(length=32), nullable=False),
        sa.Column("attribution_version", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=True),
        sa.Column("window_start", sa.DateTime(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("dataset_size", sa.Integer(), nullable=False),
        sa.Column("metric_count", sa.Integer(), nullable=False),
        sa.Column("recommendation_count", sa.Integer(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("baseline", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("source_discovery", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_learning_snapshots_tenant_generated", "learning_snapshots", ["tenant_id", "generated_at"])
    op.create_table(
        "learning_metrics",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("dimension", sa.String(length=24), nullable=False),
        sa.Column("group_key", sa.String(length=256), nullable=False),
        sa.Column("group_label", sa.String(length=256), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("positives", sa.Integer(), nullable=True),
        sa.Column("negatives", sa.Integer(), nullable=True),
        sa.Column("raw_rate", sa.Float(), nullable=True),
        sa.Column("smoothed_rate", sa.Float(), nullable=True),
        sa.Column("ci_low", sa.Float(), nullable=True),
        sa.Column("ci_high", sa.Float(), nullable=True),
        sa.Column("median_value", sa.Float(), nullable=True),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("baseline_rate", sa.Float(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("learning_version", sa.String(length=32), nullable=False),
        sa.Column("feature_version", sa.String(length=32), nullable=False),
        sa.Column("smoothing_method", sa.String(length=48), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["learning_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_learning_metrics_snapshot_id", "learning_metrics", ["snapshot_id"])
    op.create_index("ix_learning_metrics_snapshot_dimension", "learning_metrics", ["snapshot_id", "dimension", "metric"])
    op.create_table(
        "learning_recommendations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("dimension", sa.String(length=24), nullable=False),
        sa.Column("group_key", sa.String(length=256), nullable=False),
        sa.Column("group_label", sa.String(length=256), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("positives", sa.Integer(), nullable=False),
        sa.Column("observed_rate", sa.Float(), nullable=True),
        sa.Column("baseline_rate", sa.Float(), nullable=True),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("learning_version", sa.String(length=32), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=True),
        sa.Column("as_of", sa.DateTime(), nullable=True),
        sa.Column("caveat", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["learning_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_learning_recommendations_snapshot_id", "learning_recommendations", ["snapshot_id"])


def downgrade() -> None:
    op.drop_index("ix_learning_recommendations_snapshot_id", table_name="learning_recommendations")
    op.drop_table("learning_recommendations")
    op.drop_index("ix_learning_metrics_snapshot_dimension", table_name="learning_metrics")
    op.drop_index("ix_learning_metrics_snapshot_id", table_name="learning_metrics")
    op.drop_table("learning_metrics")
    op.drop_index("ix_learning_snapshots_tenant_generated", table_name="learning_snapshots")
    op.drop_table("learning_snapshots")
    with op.batch_alter_table("application_policies") as batch:
        batch.drop_column("learning_settings")
