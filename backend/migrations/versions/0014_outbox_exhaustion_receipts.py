"""Retain publication receipts when canonical recovery allowances are exhausted.

Revision ID: 0014_outbox_exhaustion_receipts
Revises: 0013_processing_outbox
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_outbox_exhaustion_receipts"
down_revision: str | Sequence[str] | None = "0013_processing_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_PUBLICATION_CHECK = (
    "(state = 'PUBLISHED' AND published_at IS NOT NULL AND attempt_count > 0) OR "
    "state = 'CANCELLED' OR (state <> 'PUBLISHED' AND published_at IS NULL)"
)
NEW_PUBLICATION_CHECK = (
    "(state = 'PUBLISHED' AND published_at IS NOT NULL AND attempt_count > 0) OR "
    "state = 'CANCELLED' OR "
    "(state = 'BLOCKED' AND published_at IS NOT NULL AND attempt_count > 0 "
    "AND last_error_category = 'ATTEMPTS_EXHAUSTED') OR "
    "(state <> 'PUBLISHED' AND published_at IS NULL)"
)


def _replace_check(expression: str) -> None:
    constraint = op.f("ck_outbox_events_consistent_publication")
    op.drop_constraint(constraint, "outbox_events", type_="check")
    op.create_check_constraint(constraint, "outbox_events", expression)


def upgrade() -> None:
    """Change only the constraint; never rewrite receipts, counters, or processing rows."""

    _replace_check(NEW_PUBLICATION_CHECK)


def downgrade() -> None:
    """Refuse to discard or mislabel exhausted publication history."""

    if op.get_bind().scalar(
        sa.text(
            "SELECT 1 FROM outbox_events WHERE state = 'BLOCKED' "
            "AND published_at IS NOT NULL LIMIT 1"
        )
    ):
        raise RuntimeError("Cannot downgrade: exhausted publication receipts exist")
    _replace_check(OLD_PUBLICATION_CHECK)
