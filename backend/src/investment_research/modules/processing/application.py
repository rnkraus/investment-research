"""Application services for creating and advancing document processing."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.contracts import ParsedDocument
from investment_research.modules.processing.domain import (
    Chunk,
    ChunkSet,
    ConfigurationVersion,
    ProcessingRun,
    ProcessingState,
    StructureBlock,
    StructureBlockType,
)


@dataclass(frozen=True, slots=True)
class ProcessingProfile:
    """Version identifiers that make one processing attempt reproducible."""

    parser_version: ConfigurationVersion
    parser_config_version: ConfigurationVersion
    chunker_version: ConfigurationVersion
    chunking_config_version: ConfigurationVersion
    lexical_config_version: ConfigurationVersion


M0_0_PROCESSING_PROFILE = ProcessingProfile(
    parser_version="pypdf-6.16.2",
    parser_config_version="m0.0-pdf-bounds-v1",
    chunker_version="m0.0-structure-block-v1",
    chunking_config_version="m0.0-structure-block-config-v1",
    lexical_config_version="m0.0-english-lexical-v1",
)


class ProcessingRunNotActiveError(ValueError):
    """Raised when lexical output is requested for a run outside PARSING."""


class NoExtractableTextError(ValueError):
    """Raised when parsed pages contain no visible text to index."""


@dataclass(frozen=True, slots=True)
class LexicalOutput:
    """One internally consistent set of canonical blocks and retrieval chunks."""

    blocks: tuple[StructureBlock, ...]
    chunk_set: ChunkSet
    chunks: tuple[Chunk, ...]


def create_captured_processing_run(
    *,
    context: WorkspaceContext,
    document_version_id: UUID,
    processing_run_id: UUID,
    created_at: datetime,
    profile: ProcessingProfile = M0_0_PROCESSING_PROFILE,
) -> ProcessingRun:
    """Create the durable processing intent associated with a captured original."""

    return ProcessingRun(
        id=processing_run_id,
        workspace_id=context.workspace_id,
        document_version_id=document_version_id,
        state=ProcessingState.CAPTURED,
        parser_version=profile.parser_version,
        parser_config_version=profile.parser_config_version,
        chunker_version=profile.chunker_version,
        chunking_config_version=profile.chunking_config_version,
        lexical_config_version=profile.lexical_config_version,
        created_at=created_at,
    )


def build_lexical_output(
    *,
    run: ProcessingRun,
    parsed_document: ParsedDocument,
    created_at: datetime,
    id_factory: Callable[[], UUID] = uuid4,
) -> LexicalOutput:
    """Map visible PDF pages to the deterministic M0.0 lexical representation."""

    if run.state is not ProcessingState.PARSING:
        raise ProcessingRunNotActiveError("lexical output requires a PARSING run")

    visible_pages = tuple(page for page in parsed_document.pages if page.text.strip())
    if not visible_pages:
        raise NoExtractableTextError("parsed document contains no extractable text")

    chunk_set = ChunkSet(
        id=id_factory(),
        workspace_id=run.workspace_id,
        processing_run_id=run.id,
        document_version_id=run.document_version_id,
        chunker_version=run.chunker_version,
        chunking_config_version=run.chunking_config_version,
        lexical_config_version=run.lexical_config_version,
        is_active=True,
        created_at=created_at,
    )
    blocks: list[StructureBlock] = []
    chunks: list[Chunk] = []
    for ordinal, page in enumerate(visible_pages):
        block = StructureBlock(
            id=id_factory(),
            workspace_id=run.workspace_id,
            processing_run_id=run.id,
            document_version_id=run.document_version_id,
            page_number=page.page_number,
            block_type=StructureBlockType.PARAGRAPH,
            ordinal=ordinal,
            text=page.text,
            start_character=0,
            end_character=len(page.text),
            created_at=created_at,
        )
        blocks.append(block)
        chunks.append(
            Chunk(
                id=id_factory(),
                workspace_id=run.workspace_id,
                processing_run_id=run.id,
                document_version_id=run.document_version_id,
                chunk_set_id=chunk_set.id,
                structure_block_id=block.id,
                ordinal=ordinal,
                text=block.text,
                token_count=len(block.text.split()),
                created_at=created_at,
            )
        )

    return LexicalOutput(
        blocks=tuple(blocks),
        chunk_set=chunk_set,
        chunks=tuple(chunks),
    )
