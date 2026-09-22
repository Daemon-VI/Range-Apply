"""opportunities_policy_queue

Blueprint Phase 2: shared opportunity identity (opportunities,
opportunity_jobs), candidate opportunity state, eligibility decisions,
priority scores, application policy and the database-backed application
queue. Links applications to tenant + opportunity.

Revision ID: c4d2f8a1e6b3
Revises: a3c1e5f7b9d2
Create Date: 2026-09-11 19:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d2f8a1e6b3"
down_revision: Union[str, None] = "a3c1e5f7b9d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "opportunities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("identity_key", sa.String(length=64), nullable=False),
        sa.Column("canonical_job_id", sa.String(length=36), nullable=True),
        sa.Column("company", sa.String(length=256), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("location_bucket", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("deadline", sa.DateTime(), nullable=True),
        sa.Column("repost_count", sa.Integer(), nullable=False),
        sa.Column("reposted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["canonical_job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key"),
    )
    op.create_index("ix_opportunities_canonical_job_id", "opportunities", ["canonical_job_id"])
    op.create_index("ix_opportunities_company", "opportunities", ["company"])
    op.create_index("ix_opportunities_status", "opportunities", ["status"])
    op.create_index("ix_opportunities_last_seen_at", "opportunities", ["last_seen_at"])

    op.create_table(
        "opportunity_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("opportunity_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("linked_by", sa.String(length=32), nullable=False),
        sa.Column("is_repost", sa.Boolean(), nullable=False),
        sa.Column("linked_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_opportunity_jobs_opportunity_id", "opportunity_jobs", ["opportunity_id"])

    op.create_table(
        "candidate_opportunities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("opportunity_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("eligibility_status", sa.String(length=16), nullable=True),
        sa.Column("eligibility_decision_id", sa.String(length=36), nullable=True),
        sa.Column("fit_score", sa.Integer(), nullable=True),
        sa.Column("fit_band", sa.String(length=8), nullable=True),
        sa.Column("match_id", sa.String(length=36), nullable=True),
        sa.Column("priority_score", sa.Integer(), nullable=True),
        sa.Column("priority_score_id", sa.String(length=36), nullable=True),
        sa.Column("application_id", sa.String(length=36), nullable=True),
        sa.Column("policy_admitted", sa.Boolean(), nullable=True),
        sa.Column("policy_reason", sa.String(length=256), nullable=True),
        sa.Column("skipped_reason", sa.String(length=256), nullable=True),
        sa.Column("state_changed_at", sa.DateTime(), nullable=False),
        sa.Column("last_evaluated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "opportunity_id", name="uq_candidate_opportunity"),
    )
    op.create_index("ix_candidate_opportunities_opportunity_id", "candidate_opportunities", ["opportunity_id"])
    op.create_index("ix_candidate_opportunities_tenant_state", "candidate_opportunities", ["tenant_id", "state"])
    op.create_index("ix_candidate_opportunities_tenant_band", "candidate_opportunities", ["tenant_id", "fit_band"])
    op.create_index("ix_candidate_opportunities_tenant_elig", "candidate_opportunities", ["tenant_id", "eligibility_status"])
    op.create_index("ix_candidate_opportunities_tenant_priority", "candidate_opportunities", ["tenant_id", "priority_score"])

    op.create_table(
        "eligibility_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_opportunity_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("job_content_hash", sa.String(length=64), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("matched_constraints", sa.JSON(), nullable=False),
        sa.Column("failed_constraints", sa.JSON(), nullable=False),
        sa.Column("uncertain_constraints", sa.JSON(), nullable=False),
        sa.Column("ruleset_version", sa.String(length=32), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["candidate_opportunity_id"], ["candidate_opportunities.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_eligibility_decisions_candidate_opportunity_id", "eligibility_decisions", ["candidate_opportunity_id"])
    op.create_index("ix_eligibility_decisions_job_id", "eligibility_decisions", ["job_id"])
    op.create_index("ix_eligibility_decisions_tenant_decision", "eligibility_decisions", ["tenant_id", "decision"])
    op.create_index("ix_eligibility_decisions_co_evaluated", "eligibility_decisions", ["candidate_opportunity_id", "evaluated_at"])

    op.create_table(
        "priority_scores",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_opportunity_id", sa.String(length=36), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("components", sa.JSON(), nullable=False),
        sa.Column("weights_version", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["candidate_opportunity_id"], ["candidate_opportunities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_priority_scores_candidate_opportunity_id", "priority_scores", ["candidate_opportunity_id"])
    op.create_index("ix_priority_scores_tenant_score", "priority_scores", ["tenant_id", "score"])

    op.create_table(
        "application_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("enabled_bands", sa.JSON(), nullable=False),
        sa.Column("band_thresholds", sa.JSON(), nullable=False),
        sa.Column("daily_cap", sa.Integer(), nullable=False),
        sa.Column("weekly_cap", sa.Integer(), nullable=False),
        sa.Column("tailoring_by_band", sa.JSON(), nullable=False),
        sa.Column("lane_by_band", sa.JSON(), nullable=False),
        sa.Column("cooldown_days", sa.Integer(), nullable=False),
        sa.Column("blocked_companies", sa.JSON(), nullable=False),
        sa.Column("preferred_locations", sa.JSON(), nullable=False),
        sa.Column("preferred_role_families", sa.JSON(), nullable=False),
        sa.Column("minimum_eligibility", sa.String(length=16), nullable=False),
        sa.Column("duplicate_policy", sa.String(length=32), nullable=False),
        sa.Column("priority_weights", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )

    op.create_table(
        "application_queue",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_opportunity_id", sa.String(length=36), nullable=False),
        sa.Column("opportunity_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("lane", sa.String(length=8), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["candidate_opportunity_id"], ["candidate_opportunities.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint(
            "tenant_id", "opportunity_id", "action", name="uq_queue_tenant_opportunity_action"
        ),
    )
    op.create_index("ix_application_queue_candidate_opportunity_id", "application_queue", ["candidate_opportunity_id"])
    op.create_index("ix_application_queue_created_at", "application_queue", ["created_at"])
    op.create_index("ix_application_queue_claim", "application_queue", ["tenant_id", "state", "available_at", "priority"])
    op.create_index("ix_application_queue_tenant_lane_state", "application_queue", ["tenant_id", "lane", "state"])

    # Link existing applications to tenant + opportunity. batch mode rebuilds
    # the table on SQLite (which cannot ADD a FK column in place); on
    # PostgreSQL it emits plain ALTER TABLE statements.
    with op.batch_alter_table("applications") as batch:
        batch.add_column(sa.Column("tenant_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("opportunity_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_applications_tenant", "tenants", ["tenant_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_foreign_key(
            "fk_applications_opportunity",
            "opportunities",
            ["opportunity_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_unique_constraint(
            "uq_applications_tenant_opportunity", ["tenant_id", "opportunity_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("applications") as batch:
        batch.drop_constraint("uq_applications_tenant_opportunity", type_="unique")
        batch.drop_constraint("fk_applications_opportunity", type_="foreignkey")
        batch.drop_constraint("fk_applications_tenant", type_="foreignkey")
        batch.drop_column("opportunity_id")
        batch.drop_column("tenant_id")
    op.drop_table("application_queue")
    op.drop_table("application_policies")
    op.drop_table("priority_scores")
    op.drop_table("eligibility_decisions")
    op.drop_table("candidate_opportunities")
    op.drop_table("opportunity_jobs")
    op.drop_table("opportunities")
