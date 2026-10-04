"""Measure the frozen M1 selector over reviewed synthetic sources without paid providers."""

import hashlib
import os
from pathlib import Path
from time import perf_counter_ns
from uuid import UUID, uuid4

from sqlalchemy import Engine

from investment_research.infrastructure.database.session import create_session_factory
from investment_research.infrastructure.search.deterministic_index import (
    DeterministicSqliteDenseIndex,
)
from investment_research.infrastructure.search.postgresql import (
    PostgresDenseCandidateQualifier,
    PostgresLexicalSearch,
)
from investment_research.modules.processing.application import M1_PROCESSING_PROFILE
from investment_research.modules.processing.domain import StructureBlock
from investment_research.modules.search.application import build_lexical_search_query
from investment_research.modules.search.contracts import (
    M1_LANE_CANDIDATE_LIMIT,
    DenseQualificationRequest,
    DenseRetrievalQuery,
    EmbeddingInput,
    EmbeddingPurpose,
    EmbeddingRequest,
    M1SelectionProfile,
)
from investment_research.modules.search.domain import RetrievalFilters
from investment_research.modules.search.selection import select_m1_context
from scripts.map_m1_5_passages import map_selected_passages
from scripts.score_m1_5_retrieval import (
    RetrievalCaseObservation,
    RetrievalConfiguration,
    RetrievalObservationSet,
    score_retrieval_observations,
)
from scripts.verify_m1_5_corpus import DEFAULT_MANIFEST, REPOSITORY_ROOT, validate_corpus
from tests.integration.factories import make_research_source
from tests.integration.m1_5 import (
    embedding_provider,
    index_sources,
    persist_sources,
    source_for_catalog_document,
)

OUTPUT_PATH_ENVIRONMENT_NAME = "INVESTMENT_RESEARCH_M1_5_RETRIEVAL_OUTPUT"


def test_reviewed_m1_5_retrieval_baseline(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    """Record observed M1 selection over all reviewed cases without generating answers."""

    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    first = make_research_source(label="M1.5 baseline workspace")
    context = first.context
    sources = tuple(
        source_for_catalog_document(document, workspace=first.workspace, context=context)
        for document in manifest.documents
    )
    persist_sources(integration_database_engine, sources)
    provider = embedding_provider()
    index = DeterministicSqliteDenseIndex(tmp_path / "dense-index.sqlite")
    index_sources(sources, provider=provider, index=index)

    source_by_catalog_id = dict(zip((doc.id for doc in manifest.documents), sources, strict=True))
    catalog_document_ids = {
        source.document.id: catalog_id for catalog_id, source in source_by_catalog_id.items()
    }
    blocks_by_id: dict[UUID, StructureBlock] = {
        block.id: block for source in sources for block in source.blocks
    }
    observations: list[RetrievalCaseObservation] = []
    session_factory = create_session_factory(integration_database_engine)
    for case in manifest.cases:
        filters = RetrievalFilters(
            document_ids=tuple(
                source_by_catalog_id[document_id].document.id
                for document_id in case.scope_document_ids
            )
        )
        start = perf_counter_ns()
        request = EmbeddingRequest(
            inputs=(
                EmbeddingInput(
                    input_id=uuid4(),
                    text=case.question,
                    purpose=EmbeddingPurpose.QUERY,
                ),
            ),
            embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
        )
        query_vector = provider.embed(request).vectors[0].values
        with session_factory() as session:
            lexical_hits = PostgresLexicalSearch(session).search(
                context,
                build_lexical_search_query(
                    query_text=case.question,
                    filters=filters,
                    lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
                    candidate_limit=M1_LANE_CANDIDATE_LIMIT,
                ),
            )
            provider_hits = tuple(
                index.retrieve(
                    context,
                    DenseRetrievalQuery(
                        query_vector=query_vector,
                        filters=filters,
                        embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
                        dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
                        candidate_limit=M1_LANE_CANDIDATE_LIMIT,
                    ),
                )
            )
            dense_hits = PostgresDenseCandidateQualifier(session).qualify(
                context,
                DenseQualificationRequest(
                    provider_hits=provider_hits,
                    filters=filters,
                    lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
                    embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
                    dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
                ),
            )
        selected = select_m1_context(
            lexical_hits=lexical_hits,
            dense_hits=dense_hits,
            profile=M1SelectionProfile(),
        ).candidates
        observations.append(
            RetrievalCaseObservation(
                case_id=case.id,
                selected_passage_ids=map_selected_passages(
                    manifest,
                    selected,
                    blocks_by_id=blocks_by_id,
                    catalog_document_ids=catalog_document_ids,
                ),
                selected_chunk_count=len(selected),
                selected_character_count=sum(len(candidate.text) for candidate in selected),
                lexical_hit_count=len(lexical_hits),
                qualified_dense_hit_count=len(dense_hits),
                elapsed_ms=(perf_counter_ns() - start) // 1_000_000,
            )
        )

    observation_set = RetrievalObservationSet(
        schema_version=1,
        corpus_id=manifest.id,
        corpus_sha256=hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest(),
        configuration=RetrievalConfiguration(
            lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
            embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
            dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
            selector_config_version=M1SelectionProfile().selector_config_version,
            embedding_adapter="deterministic_rules",
            dense_adapter="deterministic_sqlite_cosine",
        ),
        run_mode="deterministic_retrieval_offline",
        cases=tuple(observations),
    )
    summary = score_retrieval_observations(
        observation_set,
        manifest,
        hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest(),
    )
    assert summary.case_count == len(manifest.cases)
    assert summary.answerable_case_count == 5
    assert summary.evidence_available_count == 5
    output_path = os.environ.get(OUTPUT_PATH_ENVIRONMENT_NAME)
    if output_path:
        with Path(output_path).open("x", encoding="utf-8") as output:
            output.write(observation_set.model_dump_json(indent=2))
