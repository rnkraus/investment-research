"""Add an optional company ticker while preserving existing companies."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_company_ticker"
down_revision: str | Sequence[str] | None = "0017_company_link_revision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("ticker", sa.String(20, collation="C"), nullable=True))
    op.create_check_constraint("ticker_format", "companies", "ticker ~ '^[A-Z0-9][A-Z0-9.-]*$'")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE companies IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT 1 FROM companies WHERE ticker IS NOT NULL LIMIT 1")):
        raise RuntimeError("Cannot downgrade: company tickers exist")
    op.drop_constraint(op.f("ck_companies_ticker_format"), "companies", type_="check")
    op.drop_column("companies", "ticker")
