"""Unit tests for deterministic processing application services."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import (
    M1_PROCESSING_PROFILE,
    NoExtractableTextError,
    ProcessingRunNotActiveError,
    build_lexical_output,
    create_captured_processing_run,
)
from investment_research.modules.processing.contracts import ParsedDocument, ParsedPage
from investment_research.modules.processing.domain import ProcessingRun, ProcessingState

WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000001")
DOCUMENT_VERSION_ID = UUID("10000000-0000-4000-8000-000000000001")
PROCESSING_RUN_ID = UUID("20000000-0000-4000-8000-000000000001")
LEASE_ID = UUID("20000000-0000-4000-8000-000000000006")
CHUNK_SET_ID = UUID("30000000-0000-4000-8000-000000000001")
PAGE_ONE_BLOCK_ID = UUID("30000000-0000-4000-8000-000000000002")
PAGE_ONE_CHUNK_ID = UUID("30000000-0000-4000-8000-000000000003")
PAGE_THREE_BLOCK_ID = UUID("30000000-0000-4000-8000-000000000004")
PAGE_THREE_CHUNK_ID = UUID("30000000-0000-4000-8000-000000000005")
CREATED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
OUTPUT_CREATED_AT = CREATED_AT + timedelta(seconds=2)
USER_ID = UUID("00000000-0000-4000-8000-000000000002")


def make_run(state: ProcessingState = ProcessingState.PARSING) -> ProcessingRun:
    """Build one lifecycle-consistent processing run."""

    started_at = None if state is ProcessingState.CAPTURED else CREATED_AT + timedelta(seconds=1)
    completed_at = (
        OUTPUT_CREATED_AT
        if state in {ProcessingState.LEXICAL_READY, ProcessingState.FAILED}
        else None
    )
    is_captured = state is ProcessingState.CAPTURED
    is_parsing = state is ProcessingState.PARSING
    return ProcessingRun(
        id=PROCESSING_RUN_ID,
        workspace_id=WORKSPACE_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        state=state,
        parser_version="pypdf-6.16.2",
        parser_config_version="m0.0-pdf-bounds-v1",
        chunker_version="m0.0-structure-block-v1",
        chunking_config_version="m0.0-structure-block-config-v1",
        lexical_config_version="m0.0-english-lexical-v1",
        embedding_config_version="m0.0-embedding-disabled-v1",
        dense_index_config_version="m0.0-dense-index-disabled-v1",
        created_at=CREATED_AT,
        attempt_count=0 if is_captured else 1,
        lease_id=LEASE_ID if is_parsing else None,
        lease_expires_at=CREATED_AT + timedelta(seconds=46) if is_parsing else None,
        started_at=started_at,
        completed_at=completed_at,
        error_summary="Synthetic failure" if state is ProcessingState.FAILED else None,
    )


def deterministic_ids() -> Iterator[UUID]:
    """Yield the exact identifiers expected from one two-page lexical output."""

    yield from (
        CHUNK_SET_ID,
        PAGE_ONE_BLOCK_ID,
        PAGE_ONE_CHUNK_ID,
        PAGE_THREE_BLOCK_ID,
        PAGE_THREE_CHUNK_ID,
    )


def test_captured_run_snapshots_m1_dense_configuration_versions() -> None:
    run = create_captured_processing_run(
        context=WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID),
        document_version_id=DOCUMENT_VERSION_ID,
        processing_run_id=PROCESSING_RUN_ID,
        created_at=CREATED_AT,
        profile=M1_PROCESSING_PROFILE,
    )

    assert run.embedding_config_version == "openai-text-embedding-3-small-1536-v1"
    assert run.dense_index_config_version == "pinecone-serverless-cosine-1536-aws-us-east-1-v1"


def test_build_lexical_output_preserves_visible_pages_and_run_provenance() -> None:
    run = make_run()
    parsed_document = ParsedDocument(
        pages=(
            ParsedPage(page_number=1, text="  Revenue grew by 22%.\n"),
            ParsedPage(page_number=2, text=" \n\t"),
            ParsedPage(page_number=3, text="Free cash flow was positive."),
        )
    )
    identifiers = deterministic_ids()

    output = build_lexical_output(
        run=run,
        parsed_document=parsed_document,
        created_at=OUTPUT_CREATED_AT,
        id_factory=lambda: next(identifiers),
    )

    assert output.chunk_set.id == CHUNK_SET_ID
    assert output.chunk_set.workspace_id == run.workspace_id
    assert output.chunk_set.processing_run_id == run.id
    assert output.chunk_set.document_version_id == run.document_version_id
    assert output.chunk_set.chunker_version == run.chunker_version
    assert output.chunk_set.chunking_config_version == run.chunking_config_version
    assert output.chunk_set.lexical_config_version == run.lexical_config_version
    assert output.chunk_set.is_active is True

    assert tuple(block.id for block in output.blocks) == (
        PAGE_ONE_BLOCK_ID,
        PAGE_THREE_BLOCK_ID,
    )
    assert tuple(block.page_number for block in output.blocks) == (1, 3)
    assert tuple(block.ordinal for block in output.blocks) == (0, 1)
    assert output.blocks[0].text == "  Revenue grew by 22%.\n"
    assert output.blocks[0].start_character == 0
    assert output.blocks[0].end_character == len(output.blocks[0].text)

    assert tuple(chunk.id for chunk in output.chunks) == (
        PAGE_ONE_CHUNK_ID,
        PAGE_THREE_CHUNK_ID,
    )
    assert tuple(chunk.ordinal for chunk in output.chunks) == (0, 1)
    assert tuple(chunk.structure_block_id for chunk in output.chunks) == (
        PAGE_ONE_BLOCK_ID,
        PAGE_THREE_BLOCK_ID,
    )
    assert all(chunk.chunk_set_id == CHUNK_SET_ID for chunk in output.chunks)
    assert output.chunks[0].text == output.blocks[0].text
    assert output.chunks[0].token_count == 4


@pytest.mark.parametrize(
    "state",
    [
        ProcessingState.CAPTURED,
        ProcessingState.LEXICAL_READY,
        ProcessingState.FAILED,
    ],
)
def test_build_lexical_output_requires_active_parsing_run(state: ProcessingState) -> None:
    parsed_document = ParsedDocument(pages=(ParsedPage(page_number=1, text="Text"),))

    with pytest.raises(ProcessingRunNotActiveError, match="PARSING run"):
        build_lexical_output(
            run=make_run(state),
            parsed_document=parsed_document,
            created_at=OUTPUT_CREATED_AT,
        )


def test_build_lexical_output_rejects_document_without_visible_text() -> None:
    parsed_document = ParsedDocument(
        pages=(
            ParsedPage(page_number=1, text=""),
            ParsedPage(page_number=2, text=" \n\t"),
        )
    )

    with pytest.raises(NoExtractableTextError, match="no extractable text"):
        build_lexical_output(
            run=make_run(),
            parsed_document=parsed_document,
            created_at=OUTPUT_CREATED_AT,
        )
