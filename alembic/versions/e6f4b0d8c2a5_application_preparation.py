"""application_preparation

Blueprint Phase 4: tenant-scoped, opportunity-scoped application preparation
(versions, artifacts, answers), the cover-letter policy per band, and the link
from an application attempt to the preparation it will execute.

Revision ID: e6f4b0d8c2a5
Revises: d5e3a9c7b1f4
Create Date: 2026-09-12 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f4b0d8c2a5"
down_revision: Union[str, None] = "d5e3a9c7b1f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "application_preparations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_opportunity_id", sa.String(length=36), nullable=False),
        sa.Column("opportunity_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("job_content_hash", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("tailoring_level", sa.String(length=4), nullable=False),
        sa.Column("lane", sa.String(length=8), nullable=False),
        sa.Column("cover_letter_mode", sa.String(length=16), nullable=False),
        sa.Column("positioning_variant_id", sa.String(length=36), nullable=True),
        sa.Column("positioning_variant_version", sa.Integer(), nullable=True),
        sa.Column("positioning_reason", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("validation_status", sa.String(length=16), nullable=False),
        sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("evidence_keys", sa.JSON(), nullable=False),
        sa.Column("ai_used", sa.Boolean(), nullable=False),
        sa.Column("ai_provider", sa.String(length=64), nullable=True),
        sa.Column("ai_model", sa.String(length=128), nullable=True),
        sa.Column("ai_calls", sa.Integer(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["candidate_opportunity_id"], ["candidate_opportunities.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["positioning_variant_id"], ["positioning_variants.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "candidate_opportunity_id", "version", name="uq_preparation_version"
        ),
    )
    op.create_index(
        "ix_application_preparations_candidate_opportunity_id",
        "application_preparations",
        ["candidate_opportunity_id"],
    )
    op.create_index(
        "ix_application_preparations_opportunity_id", "application_preparations", ["opportunity_id"]
    )
    op.create_index("ix_preparations_tenant_status", "application_preparations", ["tenant_id", "status"])
    op.create_index(
        "ix_preparations_tenant_fingerprint", "application_preparations", ["tenant_id", "input_fingerprint"]
    )

    op.create_table(
        "preparation_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("preparation_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_type", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("blocks", sa.JSON(), nullable=False),
        sa.Column("evidence_keys", sa.JSON(), nullable=False),
        sa.Column("template_version", sa.String(length=32), nullable=False),
        sa.Column("ai_used", sa.Boolean(), nullable=False),
        sa.Column("validation_status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["preparation_id"], ["application_preparations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("preparation_id", "artifact_type", name="uq_preparation_artifact"),
    )
    op.create_index("ix_preparation_artifacts_preparation_id", "preparation_artifacts", ["preparation_id"])

    op.create_table(
        "preparation_answers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("preparation_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("question", sa.String(length=512), nullable=False),
        sa.Column("question_key", sa.String(length=512), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("evidence_keys", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("answer_bank_entry_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column("ai_used", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["preparation_id"], ["application_preparations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("preparation_id", "question_key", name="uq_preparation_question"),
    )
    op.create_index("ix_preparation_answers_preparation_id", "preparation_answers", ["preparation_id"])
    op.create_index("ix_preparation_answers_tenant_status", "preparation_answers", ["tenant_id", "status"])

    with op.batch_alter_table("application_policies") as batch:
        batch.add_column(sa.Column("cover_letter_by_band", sa.JSON(), nullable=True))

    with op.batch_alter_table("applications") as batch:
        batch.add_column(sa.Column("preparation_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_applications_preparation",
            "application_preparations",
            ["preparation_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("applications") as batch:
        batch.drop_constraint("fk_applications_preparation", type_="foreignkey")
        batch.drop_column("preparation_id")
    with op.batch_alter_table("application_policies") as batch:
        batch.drop_column("cover_letter_by_band")
    op.drop_table("preparation_answers")
    op.drop_table("preparation_artifacts")
    op.drop_table("application_preparations")
