"""discovery_volume

Blueprint Phase 3: freshness on jobs, per-observation provenance on source
references, volume/failure/resume metrics on discovery runs, and the shared
source_health table (reliability as data + polling checkpoint).

Revision ID: d5e3a9c7b1f4
Revises: c4d2f8a1e6b3
Create Date: 2026-09-11 21:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e3a9c7b1f4"
down_revision: Union[str, None] = "c4d2f8a1e6b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RUN_COUNTERS = (
    "jobs_rejected",
    "jobs_unchanged",
    "jobs_versioned",
    "jobs_reposted",
    "jobs_filtered",
    "jobs_resumed_skipped",
    "opportunities_new",
    "opportunities_linked",
    "network_requests",
    "rate_limit_hits",
    "ai_calls",
    "ai_cache_hits",
)


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(
            sa.Column("freshness", sa.String(length=16), nullable=False, server_default="UNKNOWN")
        )
    op.create_index("ix_jobs_freshness", "jobs", ["freshness"])

    with op.batch_alter_table("job_source_references") as batch:
        batch.add_column(sa.Column("content_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("fetched_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("parse_status", sa.String(length=32), nullable=True))

    with op.batch_alter_table("discovery_runs") as batch:
        batch.add_column(sa.Column("company_name", sa.String(length=256), nullable=True))
        batch.add_column(sa.Column("failure_kind", sa.String(length=32), nullable=True))
        for name in _RUN_COUNTERS:
            batch.add_column(sa.Column(name, sa.Integer(), nullable=True, server_default="0"))
        batch.add_column(sa.Column("resumed_from_run_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("checkpoint", sa.JSON(), nullable=True))

    op.create_table(
        "source_health",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_identifier", sa.String(length=256), nullable=False),
        sa.Column("company_name", sa.String(length=256), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("poll_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("next_poll_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_id", sa.String(length=36), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(), nullable=True),
        sa.Column("last_failure_kind", sa.String(length=32), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("runs_total", sa.Integer(), nullable=False),
        sa.Column("runs_success", sa.Integer(), nullable=False),
        sa.Column("runs_partial", sa.Integer(), nullable=False),
        sa.Column("runs_failed", sa.Integer(), nullable=False),
        sa.Column("runs_rate_limited", sa.Integer(), nullable=False),
        sa.Column("jobs_fetched_total", sa.Integer(), nullable=False),
        sa.Column("jobs_parsed_total", sa.Integer(), nullable=False),
        sa.Column("jobs_rejected_total", sa.Integer(), nullable=False),
        sa.Column("duplicates_total", sa.Integer(), nullable=False),
        sa.Column("last_jobs_fetched", sa.Integer(), nullable=False),
        sa.Column("last_jobs_new", sa.Integer(), nullable=False),
        sa.Column("last_fresh_posted_at", sa.DateTime(), nullable=True),
        sa.Column("avg_duration_seconds", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_identifier", name="uq_source_health_target"),
    )
    op.create_index("ix_source_health_next_poll_at", "source_health", ["next_poll_at"])


def downgrade() -> None:
    op.drop_table("source_health")
    with op.batch_alter_table("discovery_runs") as batch:
        batch.drop_column("checkpoint")
        batch.drop_column("resumed_from_run_id")
        for name in reversed(_RUN_COUNTERS):
            batch.drop_column(name)
        batch.drop_column("failure_kind")
        batch.drop_column("company_name")
    with op.batch_alter_table("job_source_references") as batch:
        batch.drop_column("parse_status")
        batch.drop_column("fetched_at")
        batch.drop_column("content_hash")
    op.drop_index("ix_jobs_freshness", table_name="jobs")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("freshness")
