"""PostgreSQL integration tests for canonical evidence-source reloading."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import Engine

from investment_research.infrastructure.database.session import create_session_factory
from investment_research.infrastructure.evidence.postgresql import (
    PostgresCanonicalEvidenceSourceReader,
)
from investment_research.modules.evidence.contracts import M1_5_DOCUMENT_ATTRIBUTION_POLICY_VERSION
from investment_research.modules.processing.persistence import (
    SqlAlchemyProcessingRepository,
    StructureBlockRow,
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
    assert result.processing_run_id == source.processing_run.id
    assert result.document_attribution_context is None


@pytest.mark.parametrize(
    "header", ["Northstar Grid Analytics\nCapacity Update\nPublished July 22, 2026", "🌱" * 801]
)
def test_document_context_reads_exact_first_prefix_from_only_the_candidate_processing_run(
    integration_database_engine: Engine,
    header: str,
) -> None:
    source = make_research_source(label="Document Attribution")
    first_text = f" \n{header}\n\nOriginal body."
    first = source.blocks[0].model_copy(
        update={"text": first_text, "end_character": len(first_text)}
    )
    source = replace(
        source,
        blocks=(first, source.blocks[1]),
        chunks=(
            source.chunks[0].model_copy(update={"text": first_text}),
            source.chunks[1],
        ),
    )
    retrieval = make_retrieval_result(source)
    persist_research_source(integration_database_engine, source)
    persist_retrieval_result(integration_database_engine, source, retrieval)
    candidate = retrieval.candidates[1]
    session_factory = create_session_factory(integration_database_engine)
    # A different processing output for the same version must never provide the prefix.
    decoy_run = source.processing_run.model_copy(update={"id": uuid4()})
    decoy_text = "Different report identity."
    decoy_block = first.model_copy(
        update={
            "id": uuid4(),
            "processing_run_id": decoy_run.id,
            "text": decoy_text,
            "end_character": len(decoy_text),
        }
    )
    with session_factory.begin() as session:
        SqlAlchemyProcessingRepository(session).add_run(source.context, decoy_run)
        session.flush()
        session.add(StructureBlockRow(**decoy_block.model_dump()))
    with session_factory() as session:
        reader = PostgresCanonicalEvidenceSourceReader(session, include_document_attribution=True)
        result = reader.get_source(source.context, retrieval.run.id, candidate.id)
        foreign_context = source.context.model_copy(update={"workspace_id": uuid4()})
        assert reader.get_source(foreign_context, retrieval.run.id, candidate.id) is None

    assert result is not None
    attribution = result.document_attribution_context
    assert attribution is not None
    assert attribution.policy_version == M1_5_DOCUMENT_ATTRIBUTION_POLICY_VERSION
    assert attribution.workspace_id == source.context.workspace_id
    assert attribution.document_id == source.document.id
    assert attribution.document_version_id == source.version.id
    assert attribution.processing_run_id == source.processing_run.id
    assert attribution.structure_block_id == first.id != result.structure_block_id
    assert attribution.text == header[:800]
    assert first.text[attribution.start_character : attribution.end_character] == attribution.text
    assert result.structure_block_text == source.blocks[1].text


@pytest.mark.parametrize("document_attribution", [False, True])
def test_reader_hides_wrong_run_unknown_and_cross_workspace_candidates(
    integration_database_engine: Engine,
    document_attribution: bool,
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
        reader = PostgresCanonicalEvidenceSourceReader(
            session, include_document_attribution=document_attribution
        )
        wrong_run = reader.get_source(source.context, uuid4(), candidate_id)
        unknown = reader.get_source(source.context, retrieval.run.id, uuid4())
        foreign_result = reader.get_source(foreign.context, retrieval.run.id, candidate_id)

    assert wrong_run is None
    assert unknown is None
    assert foreign_result is None
