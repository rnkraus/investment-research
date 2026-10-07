"""Prepare canonical source-unit catalogs in a short, provider-free read scope."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from investment_research.modules.evidence.application import ValidateGeneratedAnswerService
from investment_research.modules.evidence.ports import CanonicalEvidenceSourceReader
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import StructureBlock
from investment_research.modules.research.citable_unit_contracts import (
    CitableAnswerGenerationInput,
    CitableUnitError,
    CitableUnitSource,
)
from investment_research.modules.research.citable_units import build_citable_unit_catalog
from investment_research.modules.research.contracts import ResearchRetrievalResult


class CitableSourceReader(CanonicalEvidenceSourceReader, Protocol):
    """Reload selected candidate state and its original block in the same read scope."""

    def get_structure_block(
        self, context: WorkspaceContext, structure_block_id: UUID
    ) -> StructureBlock | None:
        """Return a detached immutable block inside the trusted workspace only."""


class CitableSourceUnitOfWork(Protocol):
    @property
    def sources(self) -> CitableSourceReader:
        """Return canonical readers bound to one short database session."""


type CitableSourceUnitOfWorkFactory = Callable[[], AbstractContextManager[CitableSourceUnitOfWork]]


@dataclass(frozen=True, slots=True)
class CitableGenerationInputBuilder:
    """Detach eligible blocks before catalog preparation or any model call."""

    unit_of_work_factory: CitableSourceUnitOfWorkFactory

    def build(
        self, context: WorkspaceContext, retrieval_result: ResearchRetrievalResult
    ) -> CitableAnswerGenerationInput:
        if (
            retrieval_result.request.workspace_id != context.workspace_id
            or retrieval_result.retrieval_run.workspace_id != context.workspace_id
            or retrieval_result.retrieval_run.research_request_id != retrieval_result.request.id
        ):
            raise CitableUnitError("source-unit preparation scope is invalid")
        sources: list[CitableUnitSource] = []
        with self.unit_of_work_factory() as unit_of_work:
            for selected in retrieval_result.sources:
                canonical = unit_of_work.sources.get_source(
                    context, retrieval_result.retrieval_run.id, selected.retrieval_candidate_id
                )
                if canonical is None or not ValidateGeneratedAnswerService.is_source_eligible(
                    context, retrieval_result.retrieval_run.id, canonical
                ):
                    raise CitableUnitError("selected source is no longer eligible")
                if (
                    canonical.retrieval_candidate_id != selected.retrieval_candidate_id
                    or canonical.chunk_id != selected.chunk_id
                    or canonical.document_id != selected.document_id
                    or canonical.document_version_id != selected.document_version_id
                    or canonical.structure_block_id != selected.structure_block_id
                    or canonical.source_locator_kind != selected.source_locator_kind
                    or canonical.page_number != selected.page_number
                    or canonical.chunk_text != selected.text
                ):
                    raise CitableUnitError("selected source does not match canonical state")
                block = unit_of_work.sources.get_structure_block(
                    context, selected.structure_block_id
                )
                if block is None or (
                    block.workspace_id != context.workspace_id
                    or block.start_character != canonical.structure_block_start_character
                    or block.end_character != canonical.structure_block_end_character
                ):
                    raise CitableUnitError("selected source has no matching canonical block")
                try:
                    sources.append(CitableUnitSource(source=selected, block=block))
                except ValidationError:
                    raise CitableUnitError("selected source has an invalid block mapping") from None
        catalog = build_citable_unit_catalog(
            context=context,
            research_request_id=retrieval_result.request.id,
            sources=tuple(sources),
        )
        return CitableAnswerGenerationInput.from_catalog(
            context=context, question=retrieval_result.request.question, catalog=catalog
        )
