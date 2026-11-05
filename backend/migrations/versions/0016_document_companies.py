"""Store reviewed document-company links and fence writes against deletion.

Revision ID: 0016_document_companies
Revises: 0015_company_identity
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_document_companies"
down_revision: str | Sequence[str] | None = "0015_company_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_companies",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("method", sa.String(16), server_default="MANUAL", nullable=False),
        sa.Column("review_status", sa.String(16), server_default="REVIEWED", nullable=False),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "document_id", "company_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "document_id"],
            ["documents.workspace_id", "documents.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "company_id"],
            ["companies.workspace_id", "companies.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("method = 'MANUAL'", name="manual_method"),
        sa.CheckConstraint("review_status = 'REVIEWED'", name="reviewed_status"),
    )
    op.execute("""
        CREATE FUNCTION guard_document_company_write()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                PERFORM require_active_document(OLD.workspace_id, OLD.document_id);
            END IF;
            PERFORM require_active_document(NEW.workspace_id, NEW.document_id);
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_document_companies_active_source
        BEFORE INSERT OR UPDATE ON document_companies
        FOR EACH ROW EXECUTE FUNCTION guard_document_company_write()
    """)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE document_companies IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT 1 FROM document_companies LIMIT 1")):
        raise RuntimeError("Cannot downgrade: document-company links exist")
    op.drop_table("document_companies")
    op.execute("DROP FUNCTION guard_document_company_write()")
