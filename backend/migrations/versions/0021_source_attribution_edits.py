"""Store owner corrections separately from immutable captured versions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_source_attribution_edits"
down_revision: str | Sequence[str] | None = "0020_document_source_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_source_attribution_edits",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("author_name", sa.String(200), nullable=True),
        sa.Column("x_handle", sa.String(16), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "document_version_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "document_version_id"],
            ["document_versions.workspace_id", "document_versions.id"],
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("LOCK TABLE document_source_attribution_edits IN ACCESS EXCLUSIVE MODE")
    )
    if connection.scalar(sa.text("SELECT 1 FROM document_source_attribution_edits LIMIT 1")):
        raise RuntimeError("Cannot downgrade: document source attribution edits exist")
    op.drop_table("document_source_attribution_edits")
