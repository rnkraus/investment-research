"""Rename source attribution columns without changing their values or types."""

from collections.abc import Sequence

from alembic import op

revision: str = "0023_source_username"
down_revision: str | Sequence[str] | None = "0022_query_planning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("document_versions", "document_source_attribution_edits")


def upgrade() -> None:
    for table in TABLES:
        op.alter_column(table, "x_handle", new_column_name="username")


def downgrade() -> None:
    for table in TABLES:
        op.alter_column(table, "username", new_column_name="x_handle")
