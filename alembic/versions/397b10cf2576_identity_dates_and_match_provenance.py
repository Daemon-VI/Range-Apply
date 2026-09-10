"""identity_dates_and_match_provenance

Adds:
* exact-comparison URL identity columns on ``jobs`` (replacing prefix LIKE
  matching in the deduplicator) plus the board identifier used to scope the
  stale-job sweep;
* source date columns (``posted_at`` already existed but was never written,
  ``source_updated_at``/``closed_at`` are new);
* score provenance on ``job_matches`` so a match is reproducible and
  explainable, and a unique index making recalculation idempotent.

Existing rows are backfilled in-place: normalized URLs are derived from the
stored URLs and the board identifier from the provenance metadata, so
deduplication behaves identically for pre-existing and newly ingested jobs.

Revision ID: 397b10cf2576
Revises: b11ea16cb71f
Create Date: 2026-09-10
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.jobs.normalization.urls import normalize_url

revision: str = "397b10cf2576"
down_revision: Union[str, None] = "b11ea16cb71f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill_jobs() -> None:
    """Derive identity columns for rows that predate this migration."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, source_url, application_url, metadata FROM jobs")
    ).fetchall()

    for row in rows:
        raw_metadata = row[3]
        if isinstance(raw_metadata, str):
            try:
                raw_metadata = json.loads(raw_metadata)
            except (ValueError, TypeError):
                raw_metadata = {}

        identifier = None
        if isinstance(raw_metadata, dict):
            identifier = (
                raw_metadata.get("board_token")
                or raw_metadata.get("company_slug")
                or raw_metadata.get("board_name")
            )

        bind.execute(
            sa.text(
                "UPDATE jobs SET normalized_source_url = :nsu, "
                "normalized_application_url = :nau, source_identifier = :ident "
                "WHERE id = :id"
            ),
            {
                "nsu": normalize_url(row[1]),
                "nau": normalize_url(row[2]),
                "ident": identifier,
                "id": row[0],
            },
        )


def upgrade() -> None:
    # --- jobs: identity + dates -------------------------------------------
    op.add_column("jobs", sa.Column("normalized_source_url", sa.String(length=1024), nullable=True))
    op.add_column("jobs", sa.Column("normalized_application_url", sa.String(length=1024), nullable=True))
    op.add_column("jobs", sa.Column("source_identifier", sa.String(length=256), nullable=True))
    op.add_column("jobs", sa.Column("source_updated_at", sa.DateTime(), nullable=True))
    op.add_column("jobs", sa.Column("closed_at", sa.DateTime(), nullable=True))

    _backfill_jobs()

    op.create_index(op.f("ix_jobs_normalized_source_url"), "jobs", ["normalized_source_url"], unique=False)
    op.create_index(
        op.f("ix_jobs_normalized_application_url"), "jobs", ["normalized_application_url"], unique=False
    )
    op.create_index(op.f("ix_jobs_source_identifier"), "jobs", ["source_identifier"], unique=False)

    # --- discovery_runs: closure accounting + trigger provenance -----------
    op.add_column(
        "discovery_runs", sa.Column("jobs_closed", sa.Integer(), nullable=True, server_default="0")
    )
    op.add_column(
        "discovery_runs",
        sa.Column("trigger", sa.String(length=32), nullable=True, server_default="manual"),
    )

    # --- match_runs: run accounting ---------------------------------------
    op.add_column("match_runs", sa.Column("jobs_matched", sa.Integer(), nullable=True, server_default="0"))
    op.add_column("match_runs", sa.Column("jobs_failed", sa.Integer(), nullable=True, server_default="0"))
    op.add_column("match_runs", sa.Column("errors", sa.JSON(), nullable=True))
    op.add_column("match_runs", sa.Column("duration_seconds", sa.Float(), nullable=True))

    # --- job_matches: score provenance ------------------------------------
    # server_default is required: these are NOT NULL columns added to a table
    # that may already contain rows.
    op.add_column("job_matches", sa.Column("job_canonical_key", sa.String(length=128), nullable=True))
    op.add_column("job_matches", sa.Column("job_content_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "job_matches",
        sa.Column("policy_version", sa.String(length=32), nullable=False, server_default="v1"),
    )
    op.add_column(
        "job_matches",
        sa.Column("engine_version", sa.String(length=32), nullable=False, server_default="1.0.0"),
    )
    op.add_column(
        "job_matches",
        sa.Column(
            "eligibility_confidence", sa.String(length=32), nullable=False, server_default="UNKNOWN"
        ),
    )
    op.add_column("job_matches", sa.Column("component_scores", sa.JSON(), nullable=True))
    op.add_column("job_matches", sa.Column("uncertainties", sa.JSON(), nullable=True))
    op.add_column("job_matches", sa.Column("evaluated_at", sa.DateTime(), nullable=True))

    op.create_index(
        op.f("ix_job_matches_eligibility_status"), "job_matches", ["eligibility_status"], unique=False
    )
    op.create_index(
        op.f("ix_job_matches_job_canonical_key"), "job_matches", ["job_canonical_key"], unique=False
    )
    op.create_index("ix_job_matches_priority_score", "job_matches", ["priority", "fit_score"], unique=False)
    op.create_index(op.f("ix_job_matches_run_id"), "job_matches", ["run_id"], unique=False)
    # Unique index (not a table constraint) so SQLite does not need a rebuild.
    op.drop_index("ix_job_matches_job_run", table_name="job_matches")
    op.create_index("ix_job_matches_job_run", "job_matches", ["job_id", "run_id"], unique=True)

    # --- requirement_assessments: weight accounting -----------------------
    op.add_column("requirement_assessments", sa.Column("requirement_original_text", sa.Text(), nullable=True))
    op.add_column(
        "requirement_assessments", sa.Column("weight", sa.Float(), nullable=True, server_default="0")
    )
    op.add_column(
        "requirement_assessments", sa.Column("contribution", sa.Float(), nullable=True, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("requirement_assessments", "contribution")
    op.drop_column("requirement_assessments", "weight")
    op.drop_column("requirement_assessments", "requirement_original_text")

    op.drop_index("ix_job_matches_job_run", table_name="job_matches")
    op.create_index("ix_job_matches_job_run", "job_matches", ["job_id", "run_id"], unique=False)
    op.drop_index(op.f("ix_job_matches_run_id"), table_name="job_matches")
    op.drop_index("ix_job_matches_priority_score", table_name="job_matches")
    op.drop_index(op.f("ix_job_matches_job_canonical_key"), table_name="job_matches")
    op.drop_index(op.f("ix_job_matches_eligibility_status"), table_name="job_matches")
    op.drop_column("job_matches", "evaluated_at")
    op.drop_column("job_matches", "uncertainties")
    op.drop_column("job_matches", "component_scores")
    op.drop_column("job_matches", "eligibility_confidence")
    op.drop_column("job_matches", "engine_version")
    op.drop_column("job_matches", "policy_version")
    op.drop_column("job_matches", "job_content_hash")
    op.drop_column("job_matches", "job_canonical_key")

    op.drop_column("match_runs", "duration_seconds")
    op.drop_column("match_runs", "errors")
    op.drop_column("match_runs", "jobs_failed")
    op.drop_column("match_runs", "jobs_matched")

    op.drop_column("discovery_runs", "trigger")
    op.drop_column("discovery_runs", "jobs_closed")

    op.drop_index(op.f("ix_jobs_source_identifier"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_normalized_application_url"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_normalized_source_url"), table_name="jobs")
    op.drop_column("jobs", "closed_at")
    op.drop_column("jobs", "source_updated_at")
    op.drop_column("jobs", "source_identifier")
    op.drop_column("jobs", "normalized_application_url")
    op.drop_column("jobs", "normalized_source_url")
