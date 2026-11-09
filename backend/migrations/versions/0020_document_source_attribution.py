"""Store optional author and X handle on the immutable source version."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_document_source_attribution"
down_revision: str | Sequence[str] | None = "0019_archive_query_embedding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("document_versions", sa.Column("author_name", sa.String(200), nullable=True))
    op.add_column("document_versions", sa.Column("x_handle", sa.String(16), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE document_versions IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(
        sa.text(
            "SELECT 1 FROM document_versions "
            "WHERE author_name IS NOT NULL OR x_handle IS NOT NULL LIMIT 1"
        )
    ):
        raise RuntimeError("Cannot downgrade: document source attribution exists")
    op.drop_column("document_versions", "x_handle")
    op.drop_column("document_versions", "author_name")
