"""Map canonical M1 retrieval and evidence records to reviewed corpus passages."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from investment_research.modules.evidence.domain import EvidenceSpan
from investment_research.modules.processing.domain import StructureBlock
from investment_research.modules.search.contracts import SelectedRetrievalCandidate
from scripts.verify_m1_5_corpus import EvaluationManifest, Passage


class PassageMappingError(Exception):
    """Reject inconsistent canonical records without exposing source text."""


def _matching_passages(
    manifest: EvaluationManifest,
    *,
    document_id: str,
    block: StructureBlock,
    source_start: int,
    source_end: int,
    require_containment: bool,
) -> tuple[str, ...]:
    matches: list[str] = []
    for passage in manifest.passages:
        anchor = passage.source_anchor
        if (
            passage.document_id != document_id
            or anchor.kind != block.source_locator_kind.value
            or anchor.page_number != block.page_number
        ):
            continue
        if require_containment:
            within_range = (
                anchor.start_character <= source_start and source_end <= anchor.end_character
            )
        else:
            within_range = (
                source_start <= anchor.start_character and anchor.end_character <= source_end
            )
        if within_range and _source_range_matches(block, passage):
            matches.append(passage.id)
    return tuple(matches)


def _source_range_matches(block: StructureBlock, passage: Passage) -> bool:
    anchor = passage.source_anchor
    relative_start = anchor.start_character - block.start_character
    relative_end = anchor.end_character - block.start_character
    return (
        0 <= relative_start < relative_end <= len(block.text)
        and block.text[relative_start:relative_end] == passage.text
    )


def map_selected_passages(
    manifest: EvaluationManifest,
    candidates: Sequence[SelectedRetrievalCandidate],
    *,
    blocks_by_id: Mapping[UUID, StructureBlock],
    catalog_document_ids: Mapping[UUID, str],
) -> tuple[str, ...]:
    """Credit only reviewed passages wholly available in a selected canonical chunk."""

    matched: set[str] = set()
    for candidate in candidates:
        block = blocks_by_id.get(candidate.structure_block_id)
        document_id = catalog_document_ids.get(candidate.document_id)
        if (
            block is None
            or document_id is None
            or candidate.document_version_id != block.document_version_id
            or candidate.source_locator_kind != block.source_locator_kind
            or candidate.page_number != block.page_number
            or candidate.text != block.text
        ):
            raise PassageMappingError("selected candidate has no coherent canonical source")
        matched.update(
            _matching_passages(
                manifest,
                document_id=document_id,
                block=block,
                source_start=block.start_character,
                source_end=block.end_character,
                require_containment=False,
            )
        )
    return tuple(passage.id for passage in manifest.passages if passage.id in matched)


def map_cited_passages(
    manifest: EvaluationManifest,
    spans: Sequence[EvidenceSpan],
    *,
    blocks_by_id: Mapping[UUID, StructureBlock],
    catalog_document_version_ids: Mapping[UUID, str],
) -> tuple[str, ...]:
    """Credit exact citations wholly inside a reviewed original-source passage."""

    matched: set[str] = set()
    for span in spans:
        block = blocks_by_id.get(span.structure_block_id)
        document_id = catalog_document_version_ids.get(span.document_version_id)
        if (
            block is None
            or document_id is None
            or span.document_version_id != block.document_version_id
            or span.source_locator_kind != block.source_locator_kind
            or span.page_number != block.page_number
            or span.end_character > len(block.text)
            or block.text[span.start_character : span.end_character] != span.exact_text
        ):
            raise PassageMappingError("evidence span has no coherent canonical source")
        source_start = block.start_character + span.start_character
        source_end = block.start_character + span.end_character
        if span.source_start_character is not None and (
            span.source_start_character != source_start or span.source_end_character != source_end
        ):
            raise PassageMappingError("evidence span does not match its original text offsets")
        matched.update(
            _matching_passages(
                manifest,
                document_id=document_id,
                block=block,
                source_start=source_start,
                source_end=source_end,
                require_containment=True,
            )
        )
    return tuple(passage.id for passage in manifest.passages if passage.id in matched)
