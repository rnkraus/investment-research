"""Immutable, request-scoped source-unit contracts without persistence or provider access."""

import json
from typing import Final, Literal, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import StructureBlock
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationSource,
    CanonicalCandidateSource,
)
from investment_research.modules.search.contracts import (
    M1_CONTEXT_CHARACTER_LIMIT,
    M1_CONTEXT_CHUNK_LIMIT,
)

M1_5_CITABLE_UNIT_POLICY_VERSION: Final = "m1-5-citable-units-en-v2"
M1_5_CITABLE_UNIT_CHARACTER_LIMIT = 800
M1_5_CITABLE_UNIT_LIMIT = 256


class CitableUnitError(ValueError):
    """A content-free catalog or reference failure; never authorizes evidence."""


class ImmutableCitableUnitContract(BaseModel):
    """Reject extra fields and hide source inputs in validation-error rendering."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class CitableUnitLimitDetails(ImmutableCitableUnitContract):
    """Content-free counters; unit_count records the first rejected unit, not the total."""

    kind: Literal["source_count", "context_characters", "unit_count", "input_metadata"]
    observed: int = Field(gt=0, strict=True)
    limit: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def require_exceeded_limit(self) -> Self:
        if self.observed <= self.limit:
            raise ValueError("source-unit diagnostics must describe an exceeded limit")
        return self


class CitableUnitLimitError(CitableUnitError):
    """Catalog preparation exceeded an application-owned bound without source disclosure."""

    def __init__(self, message: str, *, details: CitableUnitLimitDetails | None = None) -> None:
        super().__init__(message)
        self.details = details


class CitableUnitKey(ImmutableCitableUnitContract):
    """Canonical range identity, independent of retrieval lane and request handle."""

    workspace_id: UUID
    document_version_id: UUID
    structure_block_id: UUID
    start_character: int = Field(ge=0, strict=True)
    end_character: int = Field(gt=0, strict=True)
    unit_policy_version: Literal["m1-5-citable-units-en-v2"] = M1_5_CITABLE_UNIT_POLICY_VERSION

    @model_validator(mode="after")
    def require_ordered_range(self) -> Self:
        """Offsets count Unicode code points and describe a nonempty half-open range."""

        if not 0 < self.end_character - self.start_character <= M1_5_CITABLE_UNIT_CHARACTER_LIMIT:
            raise ValueError("source-unit range must contain between 1 and 800 code points")
        return self

    def request_scoped_id(self, research_request_id: UUID) -> UUID:
        """Derive a reproducible handle, not a secret or authorization capability."""

        identity = json.dumps(
            [
                str(self.workspace_id),
                str(self.document_version_id),
                str(self.structure_block_id),
                self.start_character,
                self.end_character,
                self.unit_policy_version,
            ],
            separators=(",", ":"),
        )
        return uuid5(research_request_id, identity)


class CitableUnitSource(ImmutableCitableUnitContract):
    """Pair a selected candidate with its independently loaded canonical block."""

    source: CanonicalCandidateSource = Field(repr=False)
    block: StructureBlock = Field(repr=False)

    @model_validator(mode="after")
    def require_exact_block_mapping(self) -> Self:
        """The current profile accepts only complete single-block source mappings."""

        if (
            self.source.structure_block_id != self.block.id
            or self.source.document_version_id != self.block.document_version_id
            or self.source.source_locator_kind != self.block.source_locator_kind
            or self.source.page_number != self.block.page_number
            or self.source.text != self.block.text
        ):
            raise ValueError("selected source does not match its complete canonical block")
        return self


class CitableSourceUnit(ImmutableCitableUnitContract):
    """One unchanged source slice and the candidate supplying its provenance."""

    source_unit_id: UUID
    key: CitableUnitKey
    retrieval_candidate_id: UUID
    text: str = Field(min_length=1, max_length=M1_5_CITABLE_UNIT_CHARACTER_LIMIT, repr=False)

    @model_validator(mode="after")
    def require_exact_range_length(self) -> Self:
        """Preserve whitespace inside the slice without accepting invisible units."""

        if not self.text.strip() or len(self.text) != (
            self.key.end_character - self.key.start_character
        ):
            raise ValueError("source-unit text must be visible and match its range length")
        if self.text[0].isspace() or self.text[-1].isspace():
            raise ValueError("source-unit range must exclude boundary whitespace")
        return self


class CitableUnitCatalog(ImmutableCitableUnitContract):
    """Bounded server-owned catalog; valid IDs are still unvalidated source suggestions."""

    workspace_id: UUID
    research_request_id: UUID
    unit_policy_version: Literal["m1-5-citable-units-en-v2"] = M1_5_CITABLE_UNIT_POLICY_VERSION
    sources: tuple[CitableUnitSource, ...] = Field(max_length=M1_CONTEXT_CHUNK_LIMIT, repr=False)
    units: tuple[CitableSourceUnit, ...] = Field(max_length=M1_5_CITABLE_UNIT_LIMIT, repr=False)

    @model_validator(mode="after")
    def require_complete_canonical_catalog(self) -> Self:
        """Reject mismatched scope, handles, provenance, text, order, and omitted content."""

        if sum(len(item.block.text) for item in self.sources) > M1_CONTEXT_CHARACTER_LIMIT:
            raise ValueError("source-unit catalog exceeds the context character limit")
        candidates = [item.source.retrieval_candidate_id for item in self.sources]
        chunks = [item.source.chunk_id for item in self.sources]
        blocks = [item.block.id for item in self.sources]
        if any(len(values) != len(set(values)) for values in (candidates, chunks, blocks)):
            raise ValueError("source-unit catalog sources must be unique")
        if any(item.block.workspace_id != self.workspace_id for item in self.sources):
            raise ValueError("source-unit catalog contains a foreign block")
        by_candidate = {item.source.retrieval_candidate_id: item for item in self.sources}
        source_order = {candidate: ordinal for ordinal, candidate in enumerate(candidates)}
        previous_order = -1
        ends: dict[UUID, int] = dict.fromkeys(candidates, 0)
        handles: set[UUID] = set()
        for unit in self.units:
            item = by_candidate.get(unit.retrieval_candidate_id)
            if item is None or (
                unit.key.workspace_id != self.workspace_id
                or unit.key.document_version_id != item.block.document_version_id
                or unit.key.structure_block_id != item.block.id
                or unit.source_unit_id != unit.key.request_scoped_id(self.research_request_id)
                or unit.source_unit_id in handles
            ):
                raise ValueError("source-unit catalog contains an invalid identity or provenance")
            order = source_order[unit.retrieval_candidate_id]
            start, end = unit.key.start_character, unit.key.end_character
            previous_end = ends[unit.retrieval_candidate_id]
            if (
                order < previous_order
                or start < previous_end
                or end > len(item.block.text)
                or item.block.text[previous_end:start].strip()
                or unit.text != item.block.text[start:end]
            ):
                raise ValueError("source-unit catalog contains invalid ranges or source text")
            handles.add(unit.source_unit_id)
            ends[unit.retrieval_candidate_id] = end
            previous_order = order
        if any(
            item.block.text[ends[item.source.retrieval_candidate_id] :].strip()
            for item in self.sources
        ):
            raise ValueError("source-unit catalog must retain every visible source character")
        return self


class CitableAnswerGenerationInput(AnswerGenerationInput):
    """An existing generation input bound to its trusted request-scoped unit catalog."""

    context: WorkspaceContext = Field(repr=False)
    catalog: CitableUnitCatalog = Field(repr=False)

    @model_validator(mode="after")
    def require_matching_catalog(self) -> Self:
        """Reject mismatched request, workspace, ordering, provenance, or source content."""

        if (
            self.context.workspace_id != self.catalog.workspace_id
            or self.research_request_id != self.catalog.research_request_id
            or tuple(source.model_dump() for source in self.sources)
            != tuple(item.source.model_dump() for item in self.catalog.sources)
        ):
            raise ValueError("generation input must match its current scoped source-unit catalog")
        return self

    @classmethod
    def from_catalog(
        cls,
        *,
        context: WorkspaceContext,
        question: str,
        catalog: CitableUnitCatalog,
    ) -> Self:
        """Project the catalog's retained sources onto the existing application input."""

        return cls(
            research_request_id=catalog.research_request_id,
            question=question,
            sources=tuple(
                AnswerGenerationSource.model_validate(item.source.model_dump())
                for item in catalog.sources
            ),
            context=context,
            catalog=catalog,
        )
