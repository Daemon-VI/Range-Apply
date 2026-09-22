"""document_artifacts

Blueprint Phase 8: metadata for locally rendered PDF/DOCX documents
(immutable versions, SHA-256, input fingerprint, renderer version) and the
exact artifacts an execution run uploaded.

Revision ID: b0d8f4a2c6e9
Revises: a9c7e3f1b5d8
Create Date: 2026-09-15 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b0d8f4a2c6e9"
down_revision: Union[str, None] = "a9c7e3f1b5d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_opportunity_id", sa.String(length=36), nullable=False),
        sa.Column("preparation_id", sa.String(length=36), nullable=False),
        sa.Column("preparation_version", sa.Integer(), nullable=False),
        sa.Column("artifact_type", sa.String(length=16), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("validation_status", sa.String(length=16), nullable=False),
        sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("renderer", sa.String(length=32), nullable=False),
        sa.Column("renderer_version", sa.String(length=32), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(), nullable=True),
        sa.Column("invalidated_reason", sa.String(length=256), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_opportunity_id"], ["candidate_opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["preparation_id"], ["application_preparations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("preparation_id", "artifact_type", "format", "version", name="uq_document_artifact_version"),
    )
    op.create_index("ix_document_artifacts_candidate_opportunity_id", "document_artifacts", ["candidate_opportunity_id"])
    op.create_index("ix_document_artifacts_preparation_id", "document_artifacts", ["preparation_id"])
    op.create_index("ix_document_artifacts_content_hash", "document_artifacts", ["content_hash"])
    op.create_index("ix_document_artifacts_tenant_status", "document_artifacts", ["tenant_id", "status"])

    with op.batch_alter_table("execution_runs") as batch:
        batch.add_column(sa.Column("resume_artifact_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("cover_letter_artifact_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key("fk_execution_runs_resume_artifact", "document_artifacts", ["resume_artifact_id"], ["id"], ondelete="SET NULL")
        batch.create_foreign_key("fk_execution_runs_cover_artifact", "document_artifacts", ["cover_letter_artifact_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("execution_runs") as batch:
        batch.drop_constraint("fk_execution_runs_cover_artifact", type_="foreignkey")
        batch.drop_constraint("fk_execution_runs_resume_artifact", type_="foreignkey")
        batch.drop_column("cover_letter_artifact_id")
        batch.drop_column("resume_artifact_id")
    op.drop_index("ix_document_artifacts_tenant_status", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_content_hash", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_preparation_id", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_candidate_opportunity_id", table_name="document_artifacts")
    op.drop_table("document_artifacts")
