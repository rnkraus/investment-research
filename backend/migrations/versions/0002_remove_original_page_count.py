"""Remove the premature original-artifact page count.

Revision ID: 0002_remove_original_page_count
Revises: 0001_m0_0_schema
Create Date: 2026-09-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_remove_original_page_count"
down_revision: str | Sequence[str] | None = "0001_m0_0_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Remove metadata that is unavailable until PDF processing."""

    op.drop_constraint(
        op.f("ck_original_artifacts_positive_page_count"),
        "original_artifacts",
        type_="check",
    )
    op.drop_column("original_artifacts", "page_count")


def downgrade() -> None:
    """Restore the original schema with a compatibility value for existing rows."""

    # A removed page count cannot be reconstructed. The old schema nevertheless
    # requires a positive value, so existing rows receive the smallest valid one.
    op.add_column(
        "original_artifacts",
        sa.Column("page_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.alter_column(
        "original_artifacts",
        "page_count",
        existing_type=sa.Integer(),
        server_default=None,
    )
    op.create_check_constraint(
        op.f("ck_original_artifacts_positive_page_count"),
        "original_artifacts",
        "page_count > 0",
    )
