"""Observe M1 and RRF over one frozen, canonically qualified offline corpus snapshot."""

import hashlib
import os
from collections.abc import Callable
from itertools import count
from pathlib import Path
from time import perf_counter_ns
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

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
from scripts.compare_m1_5_retrieval import (
    RetrievalComparison,
    observe_selector_pair,
    score_comparison,
)
from scripts.score_m1_5_retrieval import RetrievalConfiguration
from scripts.verify_m1_5_corpus import DEFAULT_MANIFEST, REPOSITORY_ROOT, validate_corpus
from tests.integration.factories import make_research_source
from tests.integration.m1_5 import (
    embedding_provider,
    index_sources,
    persist_sources,
    source_for_catalog_document,
)

OUTPUT_PATH_ENVIRONMENT_NAME = "INVESTMENT_RESEARCH_M1_5_RRF_COMPARISON_OUTPUT"


def stable_output_ids(corpus_sha256: str, document_id: str) -> Callable[[], UUID]:
    """Keep canonical block/chunk tie-break IDs repeatable without changing production factories."""

    namespace = uuid5(
        NAMESPACE_URL,
        (
            f"investment-research/m1.5-paired-identities-v1/{corpus_sha256}/{document_id}/"
            f"{M1_PROCESSING_PROFILE.parser_config_version}/"
            f"{M1_PROCESSING_PROFILE.chunking_config_version}"
        ),
    )
    sequence = count()
    return lambda: uuid5(namespace, str(next(sequence)))


def test_reviewed_m1_5_paired_rrf_comparison(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    """Measure selection only; provider substitutes do not establish real retrieval quality."""

    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    corpus_sha256 = hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest()
    first = make_research_source(label="M1.5 paired RRF comparison")
    sources = tuple(
        source_for_catalog_document(
            document,
            workspace=first.workspace,
            context=first.context,
            id_factory=stable_output_ids(corpus_sha256, document.id),
        )
        for document in manifest.documents
    )
    for document, source in zip(manifest.documents, sources, strict=True):
        rebuilt = source_for_catalog_document(
            document,
            workspace=first.workspace,
            context=first.context,
            id_factory=stable_output_ids(corpus_sha256, document.id),
        )
        assert [(c.id, c.text) for c in rebuilt.chunks] == [(c.id, c.text) for c in source.chunks]
    persist_sources(integration_database_engine, sources)
    provider = embedding_provider()
    index = DeterministicSqliteDenseIndex(tmp_path / "paired-index.sqlite")
    index_sources(sources, provider=provider, index=index)
    source_by_catalog_id = dict(zip((d.id for d in manifest.documents), sources, strict=True))
    catalog_document_ids = {
        source.document.id: catalog_id for catalog_id, source in source_by_catalog_id.items()
    }
    blocks_by_id = {block.id: block for source in sources for block in source.blocks}
    observations = []
    session_factory = create_session_factory(integration_database_engine)
    for case in manifest.cases:
        filters = RetrievalFilters(
            document_ids=tuple(
                source_by_catalog_id[document_id].document.id
                for document_id in case.scope_document_ids
            )
        )
        start = perf_counter_ns()
        embedding_request = EmbeddingRequest(
            inputs=(
                EmbeddingInput(
                    input_id=uuid4(), text=case.question, purpose=EmbeddingPurpose.QUERY
                ),
            ),
            embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
        )
        vector = provider.embed(embedding_request).vectors[0].values
        with session_factory() as session:
            lexical_hits = PostgresLexicalSearch(session).search(
                first.context,
                build_lexical_search_query(
                    query_text=case.question,
                    filters=filters,
                    lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
                    candidate_limit=M1_LANE_CANDIDATE_LIMIT,
                ),
            )
            raw_hits = tuple(
                index.retrieve(
                    first.context,
                    DenseRetrievalQuery(
                        query_vector=vector,
                        filters=filters,
                        embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
                        dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
                        candidate_limit=M1_LANE_CANDIDATE_LIMIT,
                    ),
                )
            )
            dense_hits = PostgresDenseCandidateQualifier(session).qualify(
                first.context,
                DenseQualificationRequest(
                    provider_hits=raw_hits,
                    filters=filters,
                    lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
                    embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
                    dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
                ),
            )
        shared_elapsed_ns = perf_counter_ns() - start
        observations.append(
            observe_selector_pair(
                case=case,
                manifest=manifest,
                lexical_hits=lexical_hits,
                dense_hits=dense_hits,
                blocks_by_id=blocks_by_id,
                catalog_document_ids=catalog_document_ids,
                shared_retrieval_elapsed_ns=shared_elapsed_ns,
            )
        )
        replay = observe_selector_pair(
            case=case,
            manifest=manifest,
            lexical_hits=lexical_hits,
            dense_hits=dense_hits,
            blocks_by_id=blocks_by_id,
            catalog_document_ids=catalog_document_ids,
            shared_retrieval_elapsed_ns=0,
        )
        assert replay.qualified_lanes_sha256 == observations[-1].qualified_lanes_sha256
        assert replay.m1.selected_chunk_ids == observations[-1].m1.selected_chunk_ids
        assert replay.rrf.selected_chunk_ids == observations[-1].rrf.selected_chunk_ids
    comparison = RetrievalComparison(
        corpus_id=manifest.id,
        corpus_sha256=corpus_sha256,
        parser_version=M1_PROCESSING_PROFILE.parser_version,
        parser_config_version=M1_PROCESSING_PROFILE.parser_config_version,
        chunker_version=M1_PROCESSING_PROFILE.chunker_version,
        chunking_config_version=M1_PROCESSING_PROFILE.chunking_config_version,
        configuration=RetrievalConfiguration(
            lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
            embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
            dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
            selector_config_version=M1SelectionProfile().selector_config_version,
            embedding_adapter="deterministic_rules",
            dense_adapter="deterministic_sqlite_cosine",
        ),
        cases=tuple(observations),
    )
    summary = score_comparison(comparison, manifest, corpus_sha256)
    for policy in ("m1", "rrf"):
        result = summary[policy]
        assert isinstance(result, dict)
        assert result["case_count"] == 6
        assert result["answerable_case_count"] == 5
        assert result["evidence_available_count"] == 5
    output_path = os.environ.get(OUTPUT_PATH_ENVIRONMENT_NAME)
    if output_path:
        with Path(output_path).open("x", encoding="utf-8") as output:
            output.write(comparison.model_dump_json(indent=2))
