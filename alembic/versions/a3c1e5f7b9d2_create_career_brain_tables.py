"""create_career_brain_tables

Blueprint Phase 1: tenant-scoped Evidence Graph (tenants, candidate_profiles,
evidence_nodes, evidence_relationships, positioning_variants,
positioning_variant_evidence, answer_bank_entries) and the generic
audit_events ledger. Seeds the default tenant so solo mode works with no
extra setup.

Revision ID: a3c1e5f7b9d2
Revises: 16c0c7e67054
Create Date: 2026-09-11 16:20:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3c1e5f7b9d2"
down_revision: Union[str, None] = "16c0c7e67054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TENANT_ID = "default"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO tenants (id, name, created_at) "
            "VALUES (:id, :name, CURRENT_TIMESTAMP)"
        ).bindparams(id=DEFAULT_TENANT_ID, name="Default tenant")
    )

    op.create_table(
        "candidate_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("email", sa.String(length=256), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("location", sa.String(length=256), nullable=True),
        sa.Column("work_authorization", sa.String(length=256), nullable=True),
        sa.Column("degree", sa.String(length=256), nullable=False),
        sa.Column("branch", sa.String(length=256), nullable=False),
        sa.Column("college", sa.String(length=256), nullable=False),
        sa.Column("graduation_year", sa.Integer(), nullable=False),
        sa.Column("current_academic_status", sa.String(length=256), nullable=False),
        sa.Column("cgpa", sa.Float(), nullable=False),
        sa.Column("backlogs", sa.String(length=128), nullable=False),
        sa.Column("github", sa.String(length=512), nullable=True),
        sa.Column("linkedin", sa.String(length=512), nullable=True),
        sa.Column("portfolio", sa.String(length=512), nullable=True),
        sa.Column("positioning_statement", sa.Text(), nullable=False),
        sa.Column("long_term_goal", sa.Text(), nullable=False),
        sa.Column("preferences", sa.JSON(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )

    op.create_table(
        "evidence_nodes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=256), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("verification_status", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("allowed_for_resume", sa.Boolean(), nullable=False),
        sa.Column("allowed_for_application", sa.Boolean(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=512), nullable=True),
        sa.Column("source_hash", sa.String(length=64), nullable=True),
        sa.Column("artifact_ref", sa.String(length=512), nullable=True),
        sa.Column("verified_by", sa.String(length=128), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_evidence_nodes_tenant_key"),
    )
    op.create_index("ix_evidence_nodes_tenant_id", "evidence_nodes", ["tenant_id"])
    op.create_index("ix_evidence_nodes_kind", "evidence_nodes", ["kind"])
    op.create_index("ix_evidence_nodes_verification_status", "evidence_nodes", ["verification_status"])
    op.create_index("ix_evidence_nodes_status", "evidence_nodes", ["status"])
    op.create_index(
        "ix_evidence_nodes_tenant_kind_status", "evidence_nodes", ["tenant_id", "kind", "status"]
    )

    op.create_table(
        "evidence_relationships",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("from_node_id", sa.String(length=36), nullable=False),
        sa.Column("to_node_id", sa.String(length=36), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_node_id"], ["evidence_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_node_id"], ["evidence_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "from_node_id", "to_node_id", "relation", name="uq_evidence_relationship"
        ),
    )
    op.create_index("ix_evidence_relationships_tenant_id", "evidence_relationships", ["tenant_id"])
    op.create_index(
        "ix_evidence_relationships_from_node_id", "evidence_relationships", ["from_node_id"]
    )
    op.create_index("ix_evidence_relationships_to_node_id", "evidence_relationships", ["to_node_id"])

    op.create_table(
        "positioning_variants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("role_family", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("headline", sa.String(length=256), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "role_family", "name", name="uq_positioning_variant_name"),
    )
    op.create_index("ix_positioning_variants_tenant_id", "positioning_variants", ["tenant_id"])
    op.create_index("ix_positioning_variants_role_family", "positioning_variants", ["role_family"])

    op.create_table(
        "positioning_variant_evidence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("variant_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["variant_id"], ["positioning_variants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["node_id"], ["evidence_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("variant_id", "node_id", name="uq_positioning_variant_node"),
    )
    op.create_index(
        "ix_positioning_variant_evidence_variant_id", "positioning_variant_evidence", ["variant_id"]
    )
    op.create_index(
        "ix_positioning_variant_evidence_node_id", "positioning_variant_evidence", ["node_id"]
    )

    op.create_table(
        "answer_bank_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("question", sa.String(length=512), nullable=False),
        sa.Column("question_key", sa.String(length=512), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("evidence_keys", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "question_key", name="uq_answer_bank_question"),
    )
    op.create_index("ix_answer_bank_entries_tenant_id", "answer_bank_entries", ["tenant_id"])
    op.create_index("ix_answer_bank_entries_category", "answer_bank_entries", ["category"])
    op.create_index("ix_answer_bank_entries_status", "answer_bank_entries", ["status"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=48), nullable=False),
        sa.Column("entity_id", sa.String(length=160), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("summary", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index("ix_audit_events_entity", "audit_events", ["tenant_id", "entity_type", "entity_id"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("answer_bank_entries")
    op.drop_table("positioning_variant_evidence")
    op.drop_table("positioning_variants")
    op.drop_table("evidence_relationships")
    op.drop_table("evidence_nodes")
    op.drop_table("candidate_profiles")
    op.drop_table("tenants")
