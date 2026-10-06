"""Tests for mapping canonical M1 source ranges to reviewed evaluation passages."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.evidence.domain import EvidenceSpan
from investment_research.modules.processing.domain import StructureBlock, StructureBlockType
from investment_research.modules.search.contracts import SelectedRetrievalCandidate
from investment_research.modules.search.domain import RetrievalOrigin
from scripts.map_m1_5_passages import (
    PassageMappingError,
    map_citation_passages,
    map_cited_passages,
    map_selected_passages,
)
from scripts.verify_m1_5_corpus import DEFAULT_MANIFEST, REPOSITORY_ROOT, EvaluationManifest

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _manifest() -> EvaluationManifest:
    return EvaluationManifest.model_validate_json(DEFAULT_MANIFEST.read_bytes())


def _source(
    *,
    text: str,
    kind: SourceLocatorKind,
    start: int = 0,
    page_number: int | None = None,
) -> tuple[UUID, UUID, StructureBlock, SelectedRetrievalCandidate]:
    document_id = uuid4()
    version_id = uuid4()
    block = StructureBlock(
        id=uuid4(),
        workspace_id=uuid4(),
        processing_run_id=uuid4(),
        document_version_id=version_id,
        source_locator_kind=kind,
        page_number=page_number,
        block_type=StructureBlockType.PARAGRAPH,
        ordinal=0,
        text=text,
        start_character=start,
        end_character=start + len(text),
        created_at=NOW,
    )
    candidate = SelectedRetrievalCandidate(
        chunk_id=uuid4(),
        document_id=document_id,
        document_version_id=version_id,
        structure_block_id=block.id,
        source_locator_kind=kind,
        page_number=page_number,
        text=text,
        origin=RetrievalOrigin.LEXICAL,
        rank=1,
        raw_score=0.5,
        selection_rank=1,
    )
    return document_id, version_id, block, candidate


def _span(
    block: StructureBlock,
    *,
    start: int,
    end: int,
) -> EvidenceSpan:
    exact_text = block.text[start:end]
    return EvidenceSpan(
        id=uuid4(),
        workspace_id=block.workspace_id,
        document_version_id=block.document_version_id,
        structure_block_id=block.id,
        source_locator_kind=block.source_locator_kind,
        page_number=block.page_number,
        source_start_character=(
            block.start_character + start
            if block.source_locator_kind is SourceLocatorKind.TEXT_PASSAGE
            else None
        ),
        source_end_character=(
            block.start_character + end
            if block.source_locator_kind is SourceLocatorKind.TEXT_PASSAGE
            else None
        ),
        start_character=start,
        end_character=end,
        exact_text=exact_text,
        text_sha256=sha256(exact_text.encode("utf-8")).hexdigest(),
        created_at=NOW,
    )


def test_pdf_page_candidate_and_exact_citation_map_to_reviewed_passage() -> None:
    manifest = _manifest()
    passage = manifest.passages[0]
    document_id, version_id, block, candidate = _source(
        text=passage.text,
        kind=SourceLocatorKind.PDF_PAGE,
        start=passage.source_anchor.start_character,
        page_number=passage.source_anchor.page_number,
    )
    selected = map_selected_passages(
        manifest,
        (candidate,),
        blocks_by_id={block.id: block},
        catalog_document_ids={document_id: passage.document_id},
    )
    cited = map_cited_passages(
        manifest,
        (_span(block, start=0, end=len(block.text)),),
        blocks_by_id={block.id: block},
        catalog_document_version_ids={version_id: passage.document_id},
    )

    assert selected == (passage.id,)
    assert cited == (passage.id,)


@pytest.mark.parametrize("reuse_span", [False, True])
def test_per_citation_mapping_preserves_repeats_and_unmatched_citations(reuse_span: bool) -> None:
    manifest = _manifest()
    passage = manifest.passages[0]
    _, version_id, block, _ = _source(
        text=passage.text + "\nUnreviewed source text.",
        kind=SourceLocatorKind.PDF_PAGE,
        start=passage.source_anchor.start_character,
        page_number=passage.source_anchor.page_number,
    )
    first = _span(block, start=0, end=10)
    second = first if reuse_span else _span(block, start=10, end=len(passage.text))
    unmatched = _span(block, start=len(passage.text) + 1, end=len(block.text))

    mappings = map_citation_passages(
        manifest,
        (first, second, unmatched),
        blocks_by_id={block.id: block},
        catalog_document_version_ids={version_id: passage.document_id},
    )

    assert mappings == ((passage.id,), (passage.id,), ())


def test_per_citation_mapping_retains_empty_output_for_no_citations() -> None:
    assert (
        map_citation_passages(_manifest(), (), blocks_by_id={}, catalog_document_version_ids={})
        == ()
    )


def test_repeated_unicode_text_is_selected_twice_but_citations_keep_their_range() -> None:
    manifest = _manifest()
    note = next(document for document in manifest.documents if "operations-note" in document.id)
    text = (REPOSITORY_ROOT / Path(note.path)).read_text(encoding="utf-8")
    document_id, version_id, block, candidate = _source(
        text=text,
        kind=SourceLocatorKind.TEXT_PASSAGE,
    )
    selected = map_selected_passages(
        manifest,
        (candidate,),
        blocks_by_id={block.id: block},
        catalog_document_ids={document_id: note.id},
    )
    budget_passages = tuple(
        passage for passage in manifest.passages if "european-pilot-budget" in passage.id
    )

    assert all(passage.id in selected for passage in budget_passages)
    for passage in budget_passages:
        anchor = passage.source_anchor
        cited = map_cited_passages(
            manifest,
            (_span(block, start=anchor.start_character, end=anchor.end_character),),
            blocks_by_id={block.id: block},
            catalog_document_version_ids={version_id: note.id},
        )
        assert cited == (passage.id,)


def test_full_chunk_citation_does_not_count_as_an_exact_reviewed_passage() -> None:
    manifest = _manifest()
    note = next(document for document in manifest.documents if "operations-note" in document.id)
    text = (REPOSITORY_ROOT / Path(note.path)).read_text(encoding="utf-8")
    _, version_id, block, _ = _source(text=text, kind=SourceLocatorKind.TEXT_PASSAGE)

    assert (
        map_cited_passages(
            manifest,
            (_span(block, start=0, end=len(text)),),
            blocks_by_id={block.id: block},
            catalog_document_version_ids={version_id: note.id},
        )
        == ()
    )


def test_candidate_with_changed_text_is_rejected() -> None:
    manifest = _manifest()
    passage = manifest.passages[0]
    document_id, _, block, candidate = _source(
        text=passage.text,
        kind=SourceLocatorKind.PDF_PAGE,
        start=passage.source_anchor.start_character,
        page_number=passage.source_anchor.page_number,
    )
    changed = candidate.model_copy(update={"text": candidate.text + " extra"})

    with pytest.raises(PassageMappingError, match="coherent canonical source"):
        map_selected_passages(
            manifest,
            (changed,),
            blocks_by_id={block.id: block},
            catalog_document_ids={document_id: passage.document_id},
        )


def test_span_with_changed_original_offset_is_rejected() -> None:
    manifest = _manifest()
    passage = manifest.passages[1]
    _, version_id, block, _ = _source(
        text=passage.text,
        kind=SourceLocatorKind.TEXT_PASSAGE,
        start=passage.source_anchor.start_character,
    )
    span = _span(block, start=0, end=len(block.text))
    assert span.source_start_character is not None
    changed = span.model_copy(update={"source_start_character": span.source_start_character + 1})

    with pytest.raises(PassageMappingError, match="original text offsets"):
        map_cited_passages(
            manifest,
            (changed,),
            blocks_by_id={block.id: block},
            catalog_document_version_ids={version_id: passage.document_id},
        )


def test_wrong_document_or_page_does_not_match() -> None:
    manifest = _manifest()
    passage = manifest.passages[0]
    document_id, _, block, candidate = _source(
        text=passage.text,
        kind=SourceLocatorKind.PDF_PAGE,
        start=passage.source_anchor.start_character,
        page_number=passage.source_anchor.page_number,
    )

    assert (
        map_selected_passages(
            manifest,
            (candidate,),
            blocks_by_id={block.id: block},
            catalog_document_ids={document_id: manifest.documents[1].id},
        )
        == ()
    )
    other_page = block.model_copy(update={"page_number": 1})
    assert (
        map_selected_passages(
            manifest,
            (candidate.model_copy(update={"page_number": 1}),),
            blocks_by_id={block.id: other_page},
            catalog_document_ids={document_id: passage.document_id},
        )
        == ()
    )
