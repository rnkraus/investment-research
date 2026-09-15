"""Add the canonical processing execution lease.

Revision ID: 0003_processing_execution_lease
Revises: 0002_remove_original_page_count
Create Date: 2026-09-15 17:41:54.880332

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_processing_execution_lease"
down_revision: str | Sequence[str] | None = "0002_remove_original_page_count"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add lease metadata and safely close unowned pre-lease processing runs."""

    op.add_column(
        "processing_runs",
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "processing_runs",
        sa.Column("lease_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "processing_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        sa.text(
            "UPDATE processing_runs SET attempt_count = 1 "
            "WHERE state IN ('PARSING', 'LEXICAL_READY', 'FAILED')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE processing_runs "
            "SET state = 'FAILED', "
            "completed_at = GREATEST(CURRENT_TIMESTAMP, started_at), "
            "error_summary = 'Processing lease unavailable after runtime upgrade' "
            "WHERE state = 'PARSING'"
        )
    )

    op.drop_constraint(
        op.f("ck_processing_runs_consistent_state"),
        "processing_runs",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_processing_runs_non_negative_attempt_count"),
        "processing_runs",
        "attempt_count >= 0",
    )
    op.create_check_constraint(
        op.f("ck_processing_runs_ordered_lease_expiry"),
        "processing_runs",
        "lease_expires_at IS NULL OR (started_at IS NOT NULL AND lease_expires_at > started_at)",
    )
    op.create_check_constraint(
        op.f("ck_processing_runs_consistent_state"),
        "processing_runs",
        "(state = 'CAPTURED' AND started_at IS NULL AND completed_at IS NULL "
        "AND error_summary IS NULL AND attempt_count = 0 "
        "AND lease_id IS NULL AND lease_expires_at IS NULL) OR "
        "(state = 'PARSING' AND started_at IS NOT NULL AND completed_at IS NULL "
        "AND error_summary IS NULL AND attempt_count > 0 "
        "AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
        "(state = 'LEXICAL_READY' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND error_summary IS NULL AND attempt_count > 0 "
        "AND lease_id IS NULL AND lease_expires_at IS NULL) OR "
        "(state = 'FAILED' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND char_length(btrim(error_summary)) > 0 AND attempt_count > 0 "
        "AND lease_id IS NULL AND lease_expires_at IS NULL)",
    )


def downgrade() -> None:
    """Remove lease metadata and restore the pre-M0 lifecycle constraint."""

    op.drop_constraint(
        op.f("ck_processing_runs_consistent_state"),
        "processing_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_processing_runs_ordered_lease_expiry"),
        "processing_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_processing_runs_non_negative_attempt_count"),
        "processing_runs",
        type_="check",
    )
    op.drop_column("processing_runs", "lease_expires_at")
    op.drop_column("processing_runs", "lease_id")
    op.drop_column("processing_runs", "attempt_count")
    op.create_check_constraint(
        op.f("ck_processing_runs_consistent_state"),
        "processing_runs",
        "(state = 'CAPTURED' AND started_at IS NULL AND completed_at IS NULL "
        "AND error_summary IS NULL) OR "
        "(state = 'PARSING' AND started_at IS NOT NULL AND completed_at IS NULL "
        "AND error_summary IS NULL) OR "
        "(state = 'LEXICAL_READY' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND error_summary IS NULL) OR "
        "(state = 'FAILED' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND char_length(btrim(error_summary)) > 0)",
    )
