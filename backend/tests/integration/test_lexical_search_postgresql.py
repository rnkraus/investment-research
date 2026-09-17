"""PostgreSQL integration tests for canonical lexical retrieval eligibility."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import Engine, select

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.infrastructure.search.postgresql import PostgresLexicalSearch
from investment_research.modules.documents.domain import OriginalMediaType, SourceLocatorKind
from investment_research.modules.documents.persistence import DocumentRow, DocumentVersionRow
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.application import build_lexical_search_query
from investment_research.modules.search.contracts import LexicalSearchHit, LexicalSearchQuery
from investment_research.modules.search.domain import RetrievalFilters
from tests.integration.factories import (
    FIXTURE_TIME,
    ResearchSource,
    make_research_source,
    persist_research_source,
)

ANSWERABLE_QUESTION = (
    "What was Northstar Grid Analytics' FY2025 revenue, and how did it change from FY2024?"
)
ANSWERABLE_TEXT = (
    "Northstar Grid Analytics FY2025 results and FY2024 change. "
    "FY2025 revenue was $184.2 million, up 22.0% from $151.0 million in FY2024."
)
UNANSWERABLE_QUESTION = "What dividend did Northstar Grid Analytics declare for FY2025?"


def with_chunk_texts(source: ResearchSource, texts: tuple[str, ...]) -> ResearchSource:
    """Replace synthetic block and chunk text while preserving their relationships."""

    assert len(texts) == len(source.blocks) == len(source.chunks)
    blocks = tuple(
        block.model_copy(
            update={
                "text": text,
                "end_character": len(text),
            }
        )
        for block, text in zip(source.blocks, texts, strict=True)
    )
    chunks = tuple(
        chunk.model_copy(
            update={
                "text": text,
                "token_count": len(text.split()),
            }
        )
        for chunk, text in zip(source.chunks, texts, strict=True)
    )
    return replace(source, blocks=blocks, chunks=chunks)


def make_query(
    source: ResearchSource,
    question: str,
    *,
    filters: RetrievalFilters | None = None,
    lexical_config_version: str | None = None,
    candidate_limit: int = 10,
) -> LexicalSearchQuery:
    """Build one query using a source's active lexical configuration by default."""

    return build_lexical_search_query(
        query_text=question,
        filters=filters or RetrievalFilters(),
        lexical_config_version=(
            lexical_config_version or source.processing_run.lexical_config_version
        ),
        candidate_limit=candidate_limit,
    )


def search(
    engine: Engine,
    context: WorkspaceContext,
    query: LexicalSearchQuery,
) -> tuple[LexicalSearchHit, ...]:
    """Execute a read-only lexical query in a short-lived session."""

    session_factory = create_session_factory(engine)
    with session_factory() as session:
        return PostgresLexicalSearch(session).search(context, query)


def test_known_passage_is_retrieved_and_unanswerable_question_returns_no_hit(
    integration_database_engine: Engine,
) -> None:
    source = with_chunk_texts(
        make_research_source(label="Lexical Acceptance"),
        (ANSWERABLE_TEXT, "Operating margin increased to 18.4 percent."),
    )
    persist_research_source(integration_database_engine, source)

    answerable_hits = search(
        integration_database_engine,
        source.context,
        make_query(source, ANSWERABLE_QUESTION),
    )
    unanswerable_hits = search(
        integration_database_engine,
        source.context,
        make_query(source, UNANSWERABLE_QUESTION),
    )

    assert [hit.chunk_id for hit in answerable_hits] == [source.chunks[0].id]
    assert answerable_hits[0].document_id == source.document.id
    assert answerable_hits[0].document_version_id == source.version.id
    assert answerable_hits[0].structure_block_id == source.blocks[0].id
    assert answerable_hits[0].source_locator_kind is SourceLocatorKind.PDF_PAGE
    assert answerable_hits[0].page_number == 1
    assert answerable_hits[0].text == ANSWERABLE_TEXT
    assert answerable_hits[0].raw_score > 0
    assert unanswerable_hits == ()


def test_pasted_text_hit_preserves_text_passage_locator(
    integration_database_engine: Engine,
) -> None:
    source = with_chunk_texts(
        make_research_source(label="Pasted Text Lexical Source"),
        (ANSWERABLE_TEXT, "Operating margin increased to 18.4 percent."),
    )
    source = replace(
        source,
        original=source.original.model_copy(
            update={
                "storage_key": source.original.storage_key.removesuffix(".pdf") + ".txt",
                "media_type": OriginalMediaType.PLAIN_TEXT,
            }
        ),
        blocks=tuple(
            block.model_copy(
                update={
                    "source_locator_kind": SourceLocatorKind.TEXT_PASSAGE,
                    "page_number": None,
                }
            )
            for block in source.blocks
        ),
    )
    persist_research_source(integration_database_engine, source)

    hits = search(
        integration_database_engine,
        source.context,
        make_query(source, ANSWERABLE_QUESTION),
    )

    assert [hit.chunk_id for hit in hits] == [source.chunks[0].id]
    assert hits[0].source_locator_kind is SourceLocatorKind.TEXT_PASSAGE
    assert hits[0].page_number is None
    assert hits[0].text == ANSWERABLE_TEXT


def test_workspace_document_configuration_and_time_filters_are_applied_in_sql(
    integration_database_engine: Engine,
) -> None:
    source = make_research_source(label="Eligible Lexical Source")
    foreign_source = make_research_source(label="Foreign Lexical Source")
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign_source)

    visible_hits = search(
        integration_database_engine,
        source.context,
        make_query(source, "FY2025 revenue"),
    )
    foreign_document_hits = search(
        integration_database_engine,
        source.context,
        make_query(
            source,
            "FY2025 revenue",
            filters=RetrievalFilters(document_ids=(foreign_source.document.id,)),
        ),
    )
    wrong_configuration_hits = search(
        integration_database_engine,
        source.context,
        make_query(
            source,
            "FY2025 revenue",
            lexical_config_version="different-english-lexical-v1",
        ),
    )
    too_early_public_hits = search(
        integration_database_engine,
        source.context,
        make_query(
            source,
            "FY2025 revenue",
            filters=RetrievalFilters(
                available_at_cutoff=FIXTURE_TIME - timedelta(seconds=1),
            ),
        ),
    )
    too_early_archive_hits = search(
        integration_database_engine,
        source.context,
        make_query(
            source,
            "FY2025 revenue",
            filters=RetrievalFilters(
                available_at_cutoff=FIXTURE_TIME + timedelta(seconds=1),
                captured_at_cutoff=FIXTURE_TIME - timedelta(seconds=1),
            ),
        ),
    )

    assert [hit.chunk_id for hit in visible_hits] == [source.chunks[0].id]
    assert foreign_source.chunks[0].id not in {hit.chunk_id for hit in visible_hits}
    assert foreign_document_hits == ()
    assert wrong_configuration_hits == ()
    assert too_early_public_hits == ()
    assert too_early_archive_hits == ()


def test_inactive_chunk_set_and_noncurrent_document_version_are_excluded(
    integration_database_engine: Engine,
) -> None:
    inactive_source = make_research_source(label="Inactive Chunk Set")
    inactive_source = replace(
        inactive_source,
        chunk_set=inactive_source.chunk_set.model_copy(update={"is_active": False}),
    )
    stale_source = make_research_source(label="Stale Document Version")
    persist_research_source(integration_database_engine, inactive_source)
    persist_research_source(integration_database_engine, stale_source)

    current_version = stale_source.version.model_copy(
        update={
            "id": uuid4(),
            "version_number": 2,
            "content_sha256": "d" * 64,
            "captured_at": FIXTURE_TIME + timedelta(seconds=1),
        }
    )
    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        session.add(DocumentVersionRow.from_domain(current_version))
        document_row = session.scalar(
            select(DocumentRow).where(DocumentRow.id == stale_source.document.id)
        )
        assert document_row is not None
        document_row.current_version_id = current_version.id

    inactive_hits = search(
        integration_database_engine,
        inactive_source.context,
        make_query(inactive_source, "FY2025 revenue"),
    )
    stale_hits = search(
        integration_database_engine,
        stale_source.context,
        make_query(stale_source, "FY2025 revenue"),
    )

    assert inactive_hits == ()
    assert stale_hits == ()


def test_equal_scores_use_chunk_id_tiebreaker_before_applying_limit(
    integration_database_engine: Engine,
) -> None:
    source = with_chunk_texts(
        make_research_source(label="Stable Lexical Ranking"),
        ("Commonmetric result.", "Commonmetric result."),
    )
    persist_research_source(integration_database_engine, source)
    expected_ids = sorted(chunk.id for chunk in source.chunks)

    all_hits = search(
        integration_database_engine,
        source.context,
        make_query(source, "commonmetric", candidate_limit=2),
    )
    limited_hits = search(
        integration_database_engine,
        source.context,
        make_query(source, "commonmetric", candidate_limit=1),
    )

    assert [hit.chunk_id for hit in all_hits] == expected_ids
    assert [hit.chunk_id for hit in limited_hits] == expected_ids[:1]
