"""Permit workspace-owned archive query embedding in the shared cost ledger."""

from collections.abc import Sequence

from alembic import op

revision: str = "0019_archive_query_embedding"
down_revision: str | Sequence[str] | None = "0018_company_ticker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_checks(*, allow_archive_queries: bool) -> None:
    research_kinds = "'QUERY_EMBEDDING', 'ANSWER_GENERATION', 'STATEMENT_SUPPORT', 'RERANKING'"
    no_owner = "research_request_id IS NULL AND processing_run_id IS NULL"
    archive_kind = "'ARCHIVE_QUERY_EMBEDDING'"
    allowed_kinds = f"'DOCUMENT_EMBEDDING', {research_kinds}"
    detached = f"owner_deleted IS TRUE AND {no_owner}"
    live = (
        "(operation = 'DOCUMENT_EMBEDDING' "
        "AND processing_run_id IS NOT NULL AND research_request_id IS NULL) OR "
        f"(operation IN ({research_kinds}) "
        "AND research_request_id IS NOT NULL AND processing_run_id IS NULL)"
    )
    if allow_archive_queries:
        allowed_kinds += f", {archive_kind}"
        detached += f" AND operation <> {archive_kind}"
        live += f" OR (operation = {archive_kind} AND {no_owner})"

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
    _replace_checks(allow_archive_queries=True)


def downgrade() -> None:
    """Old checks reject existing archive operations and preserve their accounting."""

    _replace_checks(allow_archive_queries=False)
