"""phase8b_gates_bands_ai

Blueprint Phase 8b: configuration versions on candidate opportunities (the
policy version a fit band / static admission was derived under and the
Tier-1 gate ruleset), per-tenant AI settings on the application policy, and
the ``ai_usage`` accounting table for the AI Gateway.

Revision ID: c2f0b6d4e8a1
Revises: b0d8f4a2c6e9
Create Date: 2026-09-16 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2f0b6d4e8a1"
down_revision: Union[str, None] = "b0d8f4a2c6e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("candidate_opportunities") as batch:
        batch.add_column(sa.Column("fit_policy_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("admission_policy_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("gate_ruleset_version", sa.String(length=32), nullable=True))
    with op.batch_alter_table("application_policies") as batch:
        batch.add_column(sa.Column("ai_settings", sa.JSON(), nullable=True))
    op.create_table(
        "ai_usage",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=48), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("fallback_reason", sa.String(length=256), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("gateway_version", sa.String(length=32), nullable=False),
        sa.Column("reference", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_usage_tenant_id", "ai_usage", ["tenant_id"])
    op.create_index("ix_ai_usage_tenant_created", "ai_usage", ["tenant_id", "created_at"])
    op.create_index("ix_ai_usage_tenant_operation", "ai_usage", ["tenant_id", "operation"])


def downgrade() -> None:
    op.drop_index("ix_ai_usage_tenant_operation", table_name="ai_usage")
    op.drop_index("ix_ai_usage_tenant_created", table_name="ai_usage")
    op.drop_index("ix_ai_usage_tenant_id", table_name="ai_usage")
    op.drop_table("ai_usage")
    with op.batch_alter_table("application_policies") as batch:
        batch.drop_column("ai_settings")
    with op.batch_alter_table("candidate_opportunities") as batch:
        batch.drop_column("gate_ruleset_version")
        batch.drop_column("admission_policy_version")
        batch.drop_column("fit_policy_version")
