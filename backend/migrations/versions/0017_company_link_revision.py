"""Retain an optimistic edit revision for each edited document's company set."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_company_link_revision"
down_revision: str | Sequence[str] | None = "0016_document_companies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_company_revisions",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "document_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "document_id"],
            ["documents.workspace_id", "documents.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("revision > 0", name="positive_revision"),
    )
    op.execute("""
        CREATE TRIGGER trg_document_company_revisions_active_source
        BEFORE INSERT OR UPDATE ON document_company_revisions
        FOR EACH ROW EXECUTE FUNCTION guard_document_company_write()
    """)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE document_company_revisions IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT 1 FROM document_company_revisions LIMIT 1")):
        raise RuntimeError("Cannot downgrade: company assignment revisions exist")
    op.drop_table("document_company_revisions")
