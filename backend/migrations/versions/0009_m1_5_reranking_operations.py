"""Allow research-owned M1.5 reranking operations without changing historical records.

Revision ID: 0009_m1_5_reranking_operations
Revises: 0008_m1_provider_operations

CHECK expression changes under existing names are not detected by Alembic autogenerate.
This reviewed migration explicitly replaces only the operation and owner checks.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_m1_5_reranking_operations"
down_revision: str | Sequence[str] | None = "0008_m1_provider_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_operation_checks(*, allow_reranking: bool) -> None:
    """Replace immutable historical SQL definitions, never application enum values."""

    research_operations = "'QUERY_EMBEDDING', 'ANSWER_GENERATION', 'STATEMENT_SUPPORT'"
    if allow_reranking:
        research_operations += ", 'RERANKING'"

    for name in ("ck_provider_operations_operation", "ck_provider_operations_coherent_owner"):
        op.drop_constraint(op.f(name), "provider_operations", type_="check")

    op.create_check_constraint(
        op.f("ck_provider_operations_operation"),
        "provider_operations",
        f"operation IN ('DOCUMENT_EMBEDDING', {research_operations})",
    )
    op.create_check_constraint(
        op.f("ck_provider_operations_coherent_owner"),
        "provider_operations",
        "(operation = 'DOCUMENT_EMBEDDING' "
        "AND processing_run_id IS NOT NULL AND research_request_id IS NULL) OR "
        f"(operation IN ({research_operations}) "
        "AND research_request_id IS NOT NULL AND processing_run_id IS NULL)",
    )


def upgrade() -> None:
    """Add RERANKING to the research-owned operation kinds."""

    _replace_operation_checks(allow_reranking=True)


def downgrade() -> None:
    """Restore old checks; PostgreSQL rejects this if RERANKING records still exist.

    Constraint validation and transactional DDL preserve all records on failure.
    Never delete or relabel canonical accounting merely to make a downgrade succeed.
    """

    _replace_operation_checks(allow_reranking=False)
