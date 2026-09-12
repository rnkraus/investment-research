"""PostgreSQL integration tests for canonical evidence-source reloading."""

from uuid import uuid4

from sqlalchemy import Engine

from investment_research.infrastructure.database.session import create_session_factory
from investment_research.infrastructure.evidence.postgresql import (
    PostgresCanonicalEvidenceSourceReader,
)
from tests.integration.factories import (
    make_research_source,
    make_retrieval_result,
    persist_research_source,
    persist_retrieval_result,
)


def test_reader_reloads_complete_canonical_candidate_state(
    integration_database_engine: Engine,
) -> None:
    source = make_research_source(label="Canonical Evidence Source")
    retrieval = make_retrieval_result(source)
    persist_research_source(integration_database_engine, source)
    persist_retrieval_result(integration_database_engine, source, retrieval)
    candidate = retrieval.candidates[0]

    session_factory = create_session_factory(integration_database_engine)
    with session_factory() as session:
        result = PostgresCanonicalEvidenceSourceReader(session).get_source(
            source.context,
            retrieval.run.id,
            candidate.id,
        )

    assert result is not None
    assert result.workspace_id == source.context.workspace_id
    assert result.retrieval_candidate_id == candidate.id
    assert result.retrieval_run_id == retrieval.run.id
    assert result.origin == candidate.origin
    assert result.chunk_id == source.chunks[0].id
    assert result.chunk_text == source.chunks[0].text
    assert result.chunk_set_is_active is True
    assert result.chunk_set_lexical_config_version == source.chunk_set.lexical_config_version
    assert result.processing_state == source.processing_run.state
    assert result.processing_lexical_config_version == source.processing_run.lexical_config_version
    assert result.document_id == source.document.id
    assert result.document_lifecycle_state == source.document.lifecycle_state
    assert result.document_current_version_id == source.document.current_version_id
    assert result.document_version_id == source.version.id
    assert result.version_available_at == source.version.available_at
    assert result.version_captured_at == source.version.captured_at
    assert result.structure_block_id == source.blocks[0].id
    assert result.page_number == source.blocks[0].page_number
    assert result.structure_block_text == source.blocks[0].text
    assert result.retrieval_filters == retrieval.run.filters
    assert result.retrieval_lexical_config_version == retrieval.run.lexical_config_version


def test_reader_hides_wrong_run_unknown_and_cross_workspace_candidates(
    integration_database_engine: Engine,
) -> None:
    source = make_research_source(label="Visible Evidence Source")
    foreign = make_research_source(label="Foreign Evidence Source")
    retrieval = make_retrieval_result(source)
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign)
    persist_retrieval_result(integration_database_engine, source, retrieval)
    candidate_id = retrieval.candidates[0].id

    session_factory = create_session_factory(integration_database_engine)
    with session_factory() as session:
        reader = PostgresCanonicalEvidenceSourceReader(session)
        wrong_run = reader.get_source(source.context, uuid4(), candidate_id)
        unknown = reader.get_source(source.context, retrieval.run.id, uuid4())
        foreign_result = reader.get_source(foreign.context, retrieval.run.id, candidate_id)

    assert wrong_run is None
    assert unknown is None
    assert foreign_result is None
