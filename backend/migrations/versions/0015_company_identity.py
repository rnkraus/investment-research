"""Add workspace-scoped company identity without changing existing archive records.

Revision ID: 0015_company_identity
Revises: 0014_outbox_exhaustion_receipts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_company_identity"
down_revision: str | Sequence[str] | None = "0014_outbox_exhaustion_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create an empty company store; never infer identities from existing documents."""

    op.create_table(
        "companies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("normalized_name", sa.String(length=600, collation="C"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("workspace_id", "normalized_name"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("char_length(name) > 0 AND name = btrim(name)", name="non_empty_name"),
        sa.CheckConstraint(
            "char_length(normalized_name) > 0 AND normalized_name = btrim(normalized_name)",
            name="non_empty_normalized_name",
        ),
    )


def downgrade() -> None:
    """Permit an empty rollback while refusing to discard canonical company records."""

    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE companies IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT 1 FROM companies LIMIT 1")):
        raise RuntimeError("Cannot downgrade: company records exist")
    op.drop_table("companies")
