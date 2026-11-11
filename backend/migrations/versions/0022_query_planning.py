"""Track optional query planning in the workspace provider cost ledger."""

from collections.abc import Sequence

from alembic import op

revision: str = "0022_query_planning"
down_revision: str | Sequence[str] | None = "0021_source_attribution_edits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_checks(*, allow_planning: bool) -> None:
    research_kinds = "'QUERY_EMBEDDING', 'ANSWER_GENERATION', 'STATEMENT_SUPPORT', 'RERANKING'"
    if allow_planning:
        research_kinds += ", 'QUERY_PLANNING'"
    allowed_kinds = f"'DOCUMENT_EMBEDDING', {research_kinds}, 'ARCHIVE_QUERY_EMBEDDING'"
    no_owner = "research_request_id IS NULL AND processing_run_id IS NULL"
    detached = f"owner_deleted IS TRUE AND operation <> 'ARCHIVE_QUERY_EMBEDDING' AND {no_owner}"
    live = (
        "(operation = 'DOCUMENT_EMBEDDING' "
        "AND processing_run_id IS NOT NULL AND research_request_id IS NULL) OR "
        f"(operation IN ({research_kinds}) "
        "AND research_request_id IS NOT NULL AND processing_run_id IS NULL) OR "
        f"(operation = 'ARCHIVE_QUERY_EMBEDDING' AND {no_owner})"
    )
    for name in ("ck_provider_operations_operation", "ck_provider_operations_coherent_owner"):
        op.drop_constraint(op.f(name), "provider_operations", type_="check")
    op.create_check_constraint(
        op.f("ck_provider_operations_operation"),
        "provider_operations",
        f"operation IN ({allowed_kinds})",
    )
    op.create_check_constraint(
        op.f("ck_provider_operations_coherent_owner"),
        "provider_operations",
        f"({detached}) OR (owner_deleted IS FALSE AND ({live}))",
    )


def upgrade() -> None:
    _replace_checks(allow_planning=True)


def downgrade() -> None:
    """Old checks reject existing planning operations and preserve their accounting."""

    _replace_checks(allow_planning=False)
