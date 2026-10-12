"""Permit bounded local context tuning without rewriting historical retrieval runs.

Revision ID: 0012_rag_context_bounds
Revises: 0011_document_purge_guards
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_rag_context_bounds"
down_revision: str | Sequence[str] | None = "0011_document_purge_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_context_bound(maximum: int) -> None:
    constraint = op.f("ck_retrieval_runs_bounded_context_chunk_limit")
    op.drop_constraint(constraint, "retrieval_runs", type_="check")
    op.create_check_constraint(
        constraint,
        "retrieval_runs",
        f"context_chunk_limit IS NULL OR context_chunk_limit BETWEEN 1 AND {maximum}",
    )


def upgrade() -> None:
    """Increase only the configurable count ceiling, not any existing default or text budget."""

    _replace_context_bound(32)


def downgrade() -> None:
    """Refuse to erase or mislabel historical runs that used the expanded bound."""

    if op.get_bind().scalar(
        sa.text("SELECT 1 FROM retrieval_runs WHERE context_chunk_limit > 8 LIMIT 1")
    ):
        raise RuntimeError("Cannot downgrade: retrieval history contains context limits above 8")
    _replace_context_bound(8)
