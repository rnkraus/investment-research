"""Shared canonical corpus setup for credential-free M1.5 integration baselines."""

from dataclasses import replace
from io import BytesIO
from uuid import uuid4

from sqlalchemy import Engine

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.infrastructure.parsing.media import MediaAwareDocumentParser
from investment_research.infrastructure.search.deterministic import (
    DeterministicEmbeddingRule,
    DeterministicRuleEmbeddingProvider,
    DeterministicTextMatch,
)
from investment_research.modules.documents.domain import OriginalMediaType
from investment_research.modules.documents.persistence import SqlAlchemyDocumentRepository
from investment_research.modules.identity.domain import Workspace, WorkspaceContext
from investment_research.modules.identity.persistence import SqlAlchemyWorkspaceRepository
from investment_research.modules.processing.application import (
    M1_PROCESSING_PROFILE,
    build_lexical_output,
)
from investment_research.modules.processing.contracts import ParserLimits
from investment_research.modules.processing.domain import ProcessingRun, ProcessingState
from investment_research.modules.processing.persistence import SqlAlchemyProcessingRepository
from investment_research.modules.search.application import build_dense_index_vector
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_BATCH_LIMIT,
    DenseIndexWriteRequest,
    EmbeddingInput,
    EmbeddingPurpose,
    EmbeddingRequest,
)
from investment_research.modules.search.ports import DenseIndexWriter, EmbeddingProvider
from scripts.verify_m1_5_corpus import DEFAULT_MANIFEST, REPOSITORY_ROOT, validate_corpus
from scripts.verify_m1_5_corpus import Document as CatalogDocument
from tests.integration.factories import FIXTURE_TIME, ResearchSource, make_research_source

PARSER_LIMITS = ParserLimits(max_pages=10, max_extracted_characters=100_000)


def source_for_catalog_document(
    document: CatalogDocument,
    *,
    workspace: Workspace,
    context: WorkspaceContext,
) -> ResearchSource:
    """Parse one actual catalog original into canonical M1 blocks and chunks."""

    content = (REPOSITORY_ROOT / document.path).read_bytes()
    base = make_research_source(label="M1.5 retrieval baseline")
    parsing_run = ProcessingRun.model_validate(
        base.processing_run.model_dump()
        | {
            "workspace_id": context.workspace_id,
            "state": ProcessingState.PARSING,
            "parser_version": M1_PROCESSING_PROFILE.parser_version,
            "parser_config_version": M1_PROCESSING_PROFILE.parser_config_version,
            "chunker_version": M1_PROCESSING_PROFILE.chunker_version,
            "chunking_config_version": M1_PROCESSING_PROFILE.chunking_config_version,
            "lexical_config_version": M1_PROCESSING_PROFILE.lexical_config_version,
            "embedding_config_version": M1_PROCESSING_PROFILE.embedding_config_version,
            "dense_index_config_version": M1_PROCESSING_PROFILE.dense_index_config_version,
            "started_at": FIXTURE_TIME,
            "completed_at": None,
            "attempt_count": 1,
            "lease_id": uuid4(),
            "lease_expires_at": FIXTURE_TIME.replace(year=2027),
        }
    )
    parsed = MediaAwareDocumentParser().parse(
        BytesIO(content),
        limits=PARSER_LIMITS,
        media_type=document.media_type,
    )
    lexical = build_lexical_output(
        run=parsing_run,
        parsed_document=parsed,
        created_at=FIXTURE_TIME,
    )
    searchable_run = ProcessingRun.model_validate(
        parsing_run.model_dump()
        | {
            "state": ProcessingState.SEARCHABLE,
            "completed_at": FIXTURE_TIME,
            "lease_id": None,
            "lease_expires_at": None,
        }
    )
    suffix = ".pdf" if document.media_type == "application/pdf" else ".txt"
    return replace(
        base,
        context=context,
        workspace=workspace,
        document=base.document.model_copy(update={"workspace_id": context.workspace_id}),
        version=base.version.model_copy(
            update={
                "workspace_id": context.workspace_id,
                "content_sha256": document.sha256,
            }
        ),
        original=base.original.model_copy(
            update={
                "workspace_id": context.workspace_id,
                "storage_key": (
                    f"workspaces/{context.workspace_id}/originals/{base.original.id}{suffix}"
                ),
                "media_type": OriginalMediaType(document.media_type),
                "byte_size": len(content),
                "checksum_sha256": document.sha256,
            }
        ),
        processing_run=searchable_run,
        blocks=lexical.blocks,
        chunk_set=lexical.chunk_set,
        chunks=lexical.chunks,
        request=base.request.model_copy(update={"workspace_id": context.workspace_id}),
    )


def persist_sources(engine: Engine, sources: tuple[ResearchSource, ...]) -> None:
    """Persist one common workspace and all canonical source graphs through repositories."""

    session_factory = create_session_factory(engine)
    context = sources[0].context
    with transactional_session(session_factory) as session:
        SqlAlchemyWorkspaceRepository(session).add(context, sources[0].workspace)
    for source in sources:
        with transactional_session(session_factory) as session:
            SqlAlchemyDocumentRepository(session).add_capture(
                context,
                source.document,
                source.version,
                source.original,
            )
        with transactional_session(session_factory) as session:
            SqlAlchemyProcessingRepository(session).add_run(context, source.processing_run)
        with transactional_session(session_factory) as session:
            SqlAlchemyProcessingRepository(session).add_lexical_output(
                context,
                source.blocks,
                source.chunk_set,
                source.chunks,
            )


def embedding_provider() -> DeterministicRuleEmbeddingProvider:
    """Keep the known M1 paraphrase alias explicit instead of implying model semantics."""

    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    paraphrase = next(
        case for case in manifest.cases if case.id == "question:fy2026-rollout-constraint"
    )
    passage = next(
        passage
        for passage in manifest.passages
        if passage.id == "passage:fy2026-transformer-constraint"
    )
    return DeterministicRuleEmbeddingProvider(
        embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
        rules=(
            DeterministicEmbeddingRule(
                purpose=EmbeddingPurpose.QUERY,
                text=paraphrase.question,
                match=DeterministicTextMatch.EXACT,
                dimension=0,
            ),
            DeterministicEmbeddingRule(
                purpose=EmbeddingPurpose.DOCUMENT,
                text=passage.text,
                match=DeterministicTextMatch.CONTAINS,
                dimension=0,
            ),
        ),
    )


def index_sources(
    sources: tuple[ResearchSource, ...],
    *,
    provider: EmbeddingProvider,
    index: DenseIndexWriter,
) -> None:
    """Index approved parsed chunks through injected, application-owned provider ports."""

    context = sources[0].context
    chunks = tuple((source, chunk) for source in sources for chunk in source.chunks)
    for start in range(0, len(chunks), M1_EMBEDDING_BATCH_LIMIT):
        batch = chunks[start : start + M1_EMBEDDING_BATCH_LIMIT]
        result = provider.embed(
            EmbeddingRequest(
                inputs=tuple(
                    EmbeddingInput(
                        input_id=chunk.id,
                        text=chunk.text,
                        purpose=EmbeddingPurpose.DOCUMENT,
                    )
                    for _, chunk in batch
                ),
                embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
            )
        )
        vectors = tuple(
            build_dense_index_vector(
                context,
                chunk_id=chunk.id,
                document_version_id=source.version.id,
                chunk_set_id=source.chunk_set.id,
                embedding=embedding,
                dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
            )
            for (source, chunk), embedding in zip(batch, result.vectors, strict=True)
        )
        index.upsert(
            context,
            DenseIndexWriteRequest(
                vectors=vectors,
                dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
            ),
        )
