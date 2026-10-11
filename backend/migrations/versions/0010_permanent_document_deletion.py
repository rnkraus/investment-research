"""Record irreversible deletion intent and preserve detached cost accounting.

Revision ID: 0010_permanent_document_deletion
Revises: 0009_m1_5_reranking_operations

The explicit CHECK replacements are not detected by Alembic autogeneration.
No existing document, answer, object, vector, or accounting record is deleted here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_permanent_document_deletion"
down_revision: str | Sequence[str] | None = "0009_m1_5_reranking_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_owner_check(*, allow_deleted_owner: bool) -> None:
    owner_check = (
        "(operation = 'DOCUMENT_EMBEDDING' "
        "AND processing_run_id IS NOT NULL AND research_request_id IS NULL) OR "
        "(operation IN ('QUERY_EMBEDDING', 'ANSWER_GENERATION', 'STATEMENT_SUPPORT', 'RERANKING') "
        "AND research_request_id IS NOT NULL AND processing_run_id IS NULL)"
    )
    if allow_deleted_owner:
        owner_check = (
            "(owner_deleted IS TRUE "
            "AND research_request_id IS NULL AND processing_run_id IS NULL) OR "
            f"(owner_deleted IS FALSE AND ({owner_check}))"
        )
    op.drop_constraint(
        op.f("ck_provider_operations_coherent_owner"), "provider_operations", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_provider_operations_coherent_owner"), "provider_operations", owner_check
    )


def upgrade() -> None:
    """Add empty deletion receipts and an explicit detached-accounting marker."""

    op.drop_constraint(op.f("ck_documents_document_lifecycle_state"), "documents", type_="check")
    op.create_check_constraint(
        op.f("ck_documents_document_lifecycle_state"),
        "documents",
        "lifecycle_state IN ('ACTIVE', 'PURGE_PENDING')",
    )
    op.add_column(
        "provider_operations",
        sa.Column("owner_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    _replace_owner_check(allow_deleted_owner=True)
    op.create_table(
        "document_deletions",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('PENDING', 'COMPLETED')", name=op.f("ck_document_deletions_deletion_state")
        ),
        sa.CheckConstraint(
            "(state = 'PENDING' AND completed_at IS NULL) OR "
            "(state = 'COMPLETED' AND completed_at IS NOT NULL AND completed_at >= requested_at)",
            name=op.f("ck_document_deletions_deletion_completion"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_document_deletions_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "document_id", name=op.f("pk_document_deletions")),
    )
    op.create_index(
        op.f("ix_document_deletions_state_requested_at"),
        "document_deletions",
        ["state", "requested_at"],
    )


def downgrade() -> None:
    """Refuse to erase audit history or undo an already irreversible deletion."""

    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM document_deletions)
                OR EXISTS (SELECT 1 FROM provider_operations WHERE owner_deleted IS TRUE)
                OR EXISTS (SELECT 1 FROM documents WHERE lifecycle_state = 'PURGE_PENDING') THEN
                RAISE EXCEPTION 'cannot downgrade after permanent deletion has been requested';
            END IF;
        END $$;
    """)
    _replace_owner_check(allow_deleted_owner=False)
    op.drop_column("provider_operations", "owner_deleted")
    op.drop_index(op.f("ix_document_deletions_state_requested_at"), table_name="document_deletions")
    op.drop_table("document_deletions")
    op.drop_constraint(op.f("ck_documents_document_lifecycle_state"), "documents", type_="check")
    op.create_check_constraint(
        op.f("ck_documents_document_lifecycle_state"), "documents", "lifecycle_state IN ('ACTIVE')"
    )
