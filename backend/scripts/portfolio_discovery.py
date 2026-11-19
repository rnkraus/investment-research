"""Run catalog-filtered archive discovery through the ordinary application search service."""

from time import monotonic

from investment_research.bootstrap import ApiContainer
from investment_research.modules.companies.contracts import CompanyListQuery, CompanySummary
from investment_research.modules.companies.domain import company_name_key
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.archive import ARCHIVE_PASSAGE_LIMIT, ArchiveSearchInput
from scripts.evaluation_profiles import ResolvedEvaluationProfile
from scripts.portfolio_live_runtime import LiveEvaluationError, RunBudgetExceededError
from scripts.portfolio_snapshots import (
    ArchiveMode,
    RecordedPassage,
    RecordedSelection,
    RecordedSource,
    score_selection,
)
from scripts.score_portfolio_retrieval import Case


def discovery_commands(
    container: ApiContainer, context: WorkspaceContext, resolved: ResolvedEvaluationProfile
) -> dict[tuple[str, ArchiveMode], ArchiveSearchInput]:
    """Resolve every explicit company filter uniquely before permitting a paid query."""

    cases = tuple(case for case in resolved.cases if case.kind == "discovery")
    companies: list[CompanySummary] = []
    if any(
        item.company_ticker or item.company_name
        for case in cases
        if (item := resolved.profile.archive.case_filters[case.id])
    ):
        query = CompanyListQuery(page_size=100)
        for _ in range(100):
            page = container.company_service.list_companies(context, query)
            companies.extend(page.items)
            if page.next_cursor is None:
                break
            query = CompanyListQuery(page_size=100, after=page.next_cursor)
        else:
            raise LiveEvaluationError("company lookup exceeds the bounded catalog")
    commands: dict[tuple[str, ArchiveMode], ArchiveSearchInput] = {}
    for case in cases:
        filters = resolved.profile.archive.case_filters[case.id]
        company_id = None
        if filters.company_ticker or filters.company_name:
            matches = [
                company
                for company in companies
                if (
                    company.ticker == filters.company_ticker
                    if filters.company_ticker
                    else company_name_key(company.name)
                    == company_name_key(filters.company_name or "")
                )
            ]
            if len(matches) != 1:
                raise LiveEvaluationError("discovery company filter must match exactly one company")
            company_id = matches[0].company_id
        for mode in resolved.profile.archive.modes:
            commands[(case.id, mode)] = ArchiveSearchInput(
                query=case.question,
                mode=mode,
                company_id=company_id,
                published_from=filters.published_from,
                published_to=filters.published_to,
                source_attribution=filters.source_attribution,
                source_attribution_kind=filters.source_attribution_kind,
            )
    return commands


def collect_discovery(
    container: ApiContainer,
    sources: tuple[RecordedSource, ...],
    case: Case,
    repetition: int,
    command: ArchiveSearchInput,
) -> tuple[RecordedSelection | None, dict[str, object]]:
    context = container.identity_context_provider.resolve()
    by_document = {source.document_id: source for source in sources}
    row = None
    record: dict[str, object] = {
        "case": case.id,
        "repetition": repetition,
        "archive_mode": command.mode,
        "filters": command.model_dump(mode="json", exclude={"query", "mode"}),
        "archive_lexical_strategy": "websearch",
        "manual_discovery_review_required": True,
    }
    started = monotonic()
    try:
        result = container.search_archive_service.search(context, command)
        identities = [(item.document_id, item.document_version_id) for item in result.documents] + [
            (item.document_id, item.document_version_id) for item in result.items
        ]
        for document_id, version_id in identities:
            source = by_document.get(document_id)
            if source is None or source.document_version_id != version_id:
                raise LiveEvaluationError("archive result is outside the frozen current corpus")
        row = RecordedSelection(
            case=case.id,
            question=case.question,
            repetition=repetition,
            archive_mode=command.mode,
            stage="archive_results",
            selected=tuple(
                RecordedPassage(
                    chunk_id=item.chunk_id,
                    source=by_document[item.document_id].alias,
                    page=item.page_number,
                    text=item.passage,
                    text_is_truncated=len(item.passage) == ARCHIVE_PASSAGE_LIMIT,
                )
                for item in result.items
            ),
        )
        record.update(
            status="searched",
            result=result.model_dump(mode="json"),
            retrieval_score=score_selection(case, row),
        )
    except RunBudgetExceededError:
        raise
    except Exception as error:
        record.update(status="failed", exception_type=type(error).__name__)
    record["elapsed_ms"] = round((monotonic() - started) * 1_000)
    return row, record
