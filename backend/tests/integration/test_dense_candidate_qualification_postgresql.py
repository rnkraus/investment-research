"""PostgreSQL integration tests for canonical dense-hit qualification."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import Engine, select

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.infrastructure.search.postgresql import (
    PostgresDenseCandidateQualifier,
)
from investment_research.modules.documents.persistence import DocumentRow, DocumentVersionRow
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.search.contracts import (
    DenseQualificationRequest,
    DenseRetrievalHit,
    QualifiedDenseHit,
)
from investment_research.modules.search.domain import RetrievalFilters
from tests.integration.factories import (
    FIXTURE_TIME,
    ResearchSource,
    make_searchable_research_source,
    persist_research_source,
)

LEXICAL_CONFIG_VERSION = "m1-english-lexical-v1"
EMBEDDING_CONFIG_VERSION = "openai-text-embedding-3-small-1536-v1"
DENSE_INDEX_CONFIG_VERSION = "pinecone-serverless-cosine-1536-v1"


def make_dense_source(label: str) -> ResearchSource:
    """Build one source whose active chunks have completed M1 dense indexing."""

    source = make_searchable_research_source(
        label=label,
        primary_text=f"{label} canonical dense qualification text.",
        lexical_config_version=LEXICAL_CONFIG_VERSION,
    )
    return replace(
        source,
        processing_run=source.processing_run.model_copy(
            update={
                "state": ProcessingState.SEARCHABLE,
                "embedding_config_version": EMBEDDING_CONFIG_VERSION,
                "dense_index_config_version": DENSE_INDEX_CONFIG_VERSION,
            }
        ),
    )


def make_request(
    *hits: DenseRetrievalHit,
    filters: RetrievalFilters | None = None,
    lexical_config_version: str = LEXICAL_CONFIG_VERSION,
    embedding_config_version: str = EMBEDDING_CONFIG_VERSION,
    dense_index_config_version: str = DENSE_INDEX_CONFIG_VERSION,
) -> DenseQualificationRequest:
    """Build one bounded qualification request around controlled provider hits."""

    return DenseQualificationRequest(
        provider_hits=hits,
        filters=filters or RetrievalFilters(),
        lexical_config_version=lexical_config_version,
        embedding_config_version=embedding_config_version,
        dense_index_config_version=dense_index_config_version,
    )


def qualify(
    engine: Engine,
    source: ResearchSource,
    request: DenseQualificationRequest,
) -> tuple[QualifiedDenseHit, ...]:
    """Execute one canonical read through a short-lived scoped session."""

    session_factory = create_session_factory(engine)
    with session_factory() as session:
        return PostgresDenseCandidateQualifier(session).qualify(source.context, request)


def test_qualification_preserves_provider_order_scores_and_canonical_text(
    integration_database_engine: Engine,
) -> None:
    source = make_dense_source("Ordered Dense Source")
    persist_research_source(integration_database_engine, source)
    provider_hits = (
        DenseRetrievalHit(chunk_id=source.chunks[1].id, raw_score=0.81),
        DenseRetrievalHit(chunk_id=source.chunks[0].id, raw_score=-0.12),
    )

    result = qualify(
        integration_database_engine,
        source,
        make_request(*provider_hits),
    )

    assert [hit.chunk_id for hit in result] == [hit.chunk_id for hit in provider_hits]
    assert [hit.raw_score for hit in result] == [hit.raw_score for hit in provider_hits]
    assert [hit.text for hit in result] == [source.chunks[1].text, source.chunks[0].text]
    assert all(hit.document_id == source.document.id for hit in result)
    assert all(hit.document_version_id == source.version.id for hit in result)


def test_qualification_rejects_foreign_unknown_filtered_and_misconfigured_hits(
    integration_database_engine: Engine,
) -> None:
    source = make_dense_source("Eligible Dense Source")
    foreign = make_dense_source("Foreign Dense Source")
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign)
    hits = (
        DenseRetrievalHit(chunk_id=source.chunks[0].id, raw_score=0.9),
        DenseRetrievalHit(chunk_id=foreign.chunks[0].id, raw_score=0.8),
        DenseRetrievalHit(chunk_id=uuid4(), raw_score=0.7),
    )

    visible = qualify(integration_database_engine, source, make_request(*hits))
    wrong_document = qualify(
        integration_database_engine,
        source,
        make_request(*hits, filters=RetrievalFilters(document_ids=(foreign.document.id,))),
    )
    too_early_public = qualify(
        integration_database_engine,
        source,
        make_request(
            *hits,
            filters=RetrievalFilters(available_at_cutoff=FIXTURE_TIME - timedelta(seconds=1)),
        ),
    )
    too_early_archive = qualify(
        integration_database_engine,
        source,
        make_request(
            *hits,
            filters=RetrievalFilters(
                available_at_cutoff=FIXTURE_TIME + timedelta(seconds=1),
                captured_at_cutoff=FIXTURE_TIME - timedelta(seconds=1),
            ),
        ),
    )
    wrong_lexical = qualify(
        integration_database_engine,
        source,
        make_request(*hits, lexical_config_version="other-lexical-v1"),
    )
    wrong_embedding = qualify(
        integration_database_engine,
        source,
        make_request(*hits, embedding_config_version="other-embedding-v1"),
    )
    wrong_dense_index = qualify(
        integration_database_engine,
        source,
        make_request(*hits, dense_index_config_version="other-dense-v1"),
    )

    assert [hit.chunk_id for hit in visible] == [source.chunks[0].id]
    assert wrong_document == ()
    assert too_early_public == ()
    assert too_early_archive == ()
    assert wrong_lexical == ()
    assert wrong_embedding == ()
    assert wrong_dense_index == ()


def test_qualification_rejects_inactive_chunks_and_noncurrent_versions(
    integration_database_engine: Engine,
) -> None:
    inactive = make_dense_source("Inactive Dense Source")
    inactive = replace(
        inactive,
        chunk_set=inactive.chunk_set.model_copy(update={"is_active": False}),
    )
    stale = make_dense_source("Stale Dense Source")
    persist_research_source(integration_database_engine, inactive)
    persist_research_source(integration_database_engine, stale)

    current_version = stale.version.model_copy(
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
            select(DocumentRow).where(DocumentRow.id == stale.document.id)
        )
        assert document_row is not None
        document_row.current_version_id = current_version.id

    inactive_result = qualify(
        integration_database_engine,
        inactive,
        make_request(DenseRetrievalHit(chunk_id=inactive.chunks[0].id, raw_score=0.9)),
    )
    stale_result = qualify(
        integration_database_engine,
        stale,
        make_request(DenseRetrievalHit(chunk_id=stale.chunks[0].id, raw_score=0.9)),
    )

    assert inactive_result == ()
    assert stale_result == ()
