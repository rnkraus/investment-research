"""Deterministically prepare and resolve exact source units without provider or database calls."""

import re
from collections.abc import Iterator
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.research.citable_unit_contracts import (
    M1_5_CITABLE_UNIT_CHARACTER_LIMIT,
    M1_5_CITABLE_UNIT_LIMIT,
    CitableSourceUnit,
    CitableUnitCatalog,
    CitableUnitError,
    CitableUnitKey,
    CitableUnitLimitDetails,
    CitableUnitLimitError,
    CitableUnitSource,
)
from investment_research.modules.research.contracts import (
    M1_STATEMENT_EVIDENCE_LIMIT,
    UnvalidatedEvidenceReference,
)
from investment_research.modules.search.contracts import (
    M1_CONTEXT_CHARACTER_LIMIT,
    M1_CONTEXT_CHUNK_LIMIT,
)

# This versioned heuristic preserves PDF wrapping; it is not a linguistic sentence parser.
_UNIT_BOUNDARY = re.compile(r"[.?!]+[\"'\u201d\u2019)\]}]*(?=\s|$)|\n[ \t]*\n")


def _bounded_visible_ranges(text: str, start: int, end: int) -> Iterator[tuple[int, int]]:
    """Split a range at whitespace or the hard limit without rewriting any character."""

    while start < end:
        while start < end and text[start].isspace():
            start += 1
        if start == end:
            return
        split = min(start + M1_5_CITABLE_UNIT_CHARACTER_LIMIT, end)
        if split < end:
            whitespace = next(
                (position for position in range(split, start, -1) if text[position].isspace()),
                None,
            )
            if whitespace is not None:
                split = whitespace
        visible_end = split
        while visible_end > start and text[visible_end - 1].isspace():
            visible_end -= 1
        yield start, visible_end
        start = split


def _source_ranges(text: str) -> Iterator[tuple[int, int]]:
    """Keep punctuation/closing characters and trim separators by moving offsets only."""

    start = 0
    for boundary in _UNIT_BOUNDARY.finditer(text):
        end = boundary.end()
        yield from _bounded_visible_ranges(text, start, end)
        start = end
    yield from _bounded_visible_ranges(text, start, len(text))


def build_citable_unit_catalog(
    *,
    context: WorkspaceContext,
    research_request_id: UUID,
    sources: tuple[CitableUnitSource, ...],
) -> CitableUnitCatalog:
    """Prepare units from already-qualified selected sources, not new authorization proof."""

    if len(sources) > M1_CONTEXT_CHUNK_LIMIT:
        raise CitableUnitLimitError(
            "source-unit preparation exceeds the source limit",
            details=CitableUnitLimitDetails(
                kind="source_count", observed=len(sources), limit=M1_CONTEXT_CHUNK_LIMIT
            ),
        )
    characters = sum(len(item.block.text) for item in sources)
    if characters > M1_CONTEXT_CHARACTER_LIMIT:
        raise CitableUnitLimitError(
            "source-unit preparation exceeds the context character limit",
            details=CitableUnitLimitDetails(
                kind="context_characters", observed=characters, limit=M1_CONTEXT_CHARACTER_LIMIT
            ),
        )

    retained: list[CitableUnitSource] = []
    units: list[CitableSourceUnit] = []
    by_block: dict[UUID, CitableUnitSource] = {}
    by_candidate: dict[UUID, CitableUnitSource] = {}
    for item in sources:
        if item.block.workspace_id != context.workspace_id:
            raise CitableUnitError("source-unit preparation contains a foreign source")
        candidate_id = item.source.retrieval_candidate_id
        previous_candidate = by_candidate.get(candidate_id)
        if previous_candidate is not None and previous_candidate != item:
            raise CitableUnitError("source-unit preparation has inconsistent candidate provenance")
        by_candidate[candidate_id] = item
        previous_block = by_block.get(item.block.id)
        if previous_block is not None:
            if (
                previous_block.block != item.block
                or previous_block.source.document_id != item.source.document_id
            ):
                raise CitableUnitError("source-unit preparation has inconsistent block provenance")
            # Both selected lanes can expose one complete block. The first owns provenance.
            continue
        by_block[item.block.id] = item
        retained.append(item)
        for start, end in _source_ranges(item.block.text):
            if len(units) == M1_5_CITABLE_UNIT_LIMIT:
                raise CitableUnitLimitError(
                    "source-unit preparation exceeds the unit limit",
                    details=CitableUnitLimitDetails(
                        kind="unit_count", observed=len(units) + 1, limit=M1_5_CITABLE_UNIT_LIMIT
                    ),
                )
            key = CitableUnitKey(
                workspace_id=context.workspace_id,
                document_version_id=item.block.document_version_id,
                structure_block_id=item.block.id,
                start_character=start,
                end_character=end,
            )
            units.append(
                CitableSourceUnit(
                    source_unit_id=key.request_scoped_id(research_request_id),
                    key=key,
                    retrieval_candidate_id=candidate_id,
                    text=item.block.text[start:end],
                )
            )
    return CitableUnitCatalog(
        workspace_id=context.workspace_id,
        research_request_id=research_request_id,
        sources=tuple(retained),
        units=tuple(units),
    )


def resolve_citable_unit_ids(
    *,
    context: WorkspaceContext,
    research_request_id: UUID,
    catalog: CitableUnitCatalog,
    source_unit_ids: tuple[UUID, ...],
) -> tuple[UnvalidatedEvidenceReference, ...]:
    """Resolve one statement's supplied IDs without fuzzy matching or evidence authorization."""

    if (
        catalog.workspace_id != context.workspace_id
        or catalog.research_request_id != research_request_id
    ):
        raise CitableUnitError("source-unit catalog does not belong to the current request scope")
    if not 1 <= len(source_unit_ids) <= M1_STATEMENT_EVIDENCE_LIMIT:
        raise CitableUnitError("a statement must select one or two source units")
    if len(source_unit_ids) != len(set(source_unit_ids)):
        raise CitableUnitError("a statement must select distinct source units")
    by_id = {unit.source_unit_id: unit for unit in catalog.units}
    references: list[UnvalidatedEvidenceReference] = []
    for source_unit_id in source_unit_ids:
        unit = by_id.get(source_unit_id)
        if unit is None:
            raise CitableUnitError("selected source unit is not in the current catalog")
        references.append(
            UnvalidatedEvidenceReference(
                retrieval_candidate_id=unit.retrieval_candidate_id,
                start_character=unit.key.start_character,
                end_character=unit.key.end_character,
                exact_text=unit.text,
            )
        )
    return tuple(references)
