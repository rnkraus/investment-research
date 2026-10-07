"""Provider-free catalog preparation rejects stale or mismatched canonical snapshots."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest

from investment_research.modules.evidence.contracts import CanonicalEvidenceSource
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import StructureBlock, StructureBlockType
from investment_research.modules.research.citable_generation import (
    CitableGenerationInputBuilder,
    CitableSourceUnitOfWork,
)
from investment_research.modules.research.citable_unit_contracts import (
    CitableUnitError,
    CitableUnitLimitError,
)
from investment_research.modules.research.domain import AnswerState
from investment_research.modules.search.domain import RetrievalFilters
from tests.unit.test_validated_answer_generation import (
    RETRIEVED_AT,
    RecordingAnswerValidationUnitOfWork,
    make_answer_proposal,
    make_canonical_source,
    make_context,
    make_retrieval_result,
    make_service,
)


@dataclass
class SourceScope:
    canonical: CanonicalEvidenceSource | None
    block: StructureBlock | None
    opened: bool = False
    reads: int = 0

    @property
    def sources(self) -> SourceScope:
        return self

    def get_source(
        self, context: WorkspaceContext, retrieval_run_id: object, retrieval_candidate_id: object
    ) -> CanonicalEvidenceSource | None:
        assert self.opened
        self.reads += 1
        return self.canonical

    def get_structure_block(
        self, context: WorkspaceContext, structure_block_id: object
    ) -> StructureBlock | None:
        assert self.opened
        return self.block

    @contextmanager
    def open(self) -> Iterator[CitableSourceUnitOfWork]:
        self.opened = True
        try:
            yield self
        finally:
            self.opened = False


def make_scope() -> SourceScope:
    canonical = make_canonical_source()
    block = StructureBlock(
        id=canonical.structure_block_id,
        workspace_id=canonical.workspace_id,
        document_version_id=canonical.document_version_id,
        processing_run_id=uuid4(),
        source_locator_kind=canonical.source_locator_kind,
        page_number=canonical.page_number,
        block_type=StructureBlockType.PARAGRAPH,
        ordinal=0,
        text=canonical.structure_block_text,
        start_character=canonical.structure_block_start_character,
        end_character=canonical.structure_block_end_character,
        created_at=RETRIEVED_AT,
    )
    return SourceScope(canonical, block)


def test_catalog_is_detached_and_preserves_selected_canonical_text() -> None:
    scope = make_scope()
    request = CitableGenerationInputBuilder(scope.open).build(
        make_context(), make_retrieval_result()
    )
    assert not scope.opened
    assert request.sources[0].text == make_retrieval_result().sources[0].text
    assert request.catalog.units
    assert request.catalog.sources[0].block == scope.block


@pytest.mark.parametrize(
    "updates",
    [
        {"workspace_id": uuid4()},
        {"retrieval_run_id": uuid4()},
        {"chunk_set_is_active": False},
        {"document_lifecycle_state": "DELETED"},
        {"document_current_version_id": uuid4()},
        {"chunk_id": uuid4()},
        {"document_id": uuid4()},
        {"structure_block_text": "Changed canonical text."},
        {"retrieval_filters": RetrievalFilters(document_ids=(uuid4(),))},
        {
            "retrieval_filters": RetrievalFilters(
                available_at_cutoff=RETRIEVED_AT - timedelta(days=1)
            )
        },
        {
            "retrieval_filters": RetrievalFilters(
                available_at_cutoff=RETRIEVED_AT,
                captured_at_cutoff=RETRIEVED_AT - timedelta(days=1),
            )
        },
    ],
)
def test_ineligible_or_mismatched_source_fails_without_content(updates: dict[str, object]) -> None:
    scope = make_scope()
    assert scope.canonical is not None
    scope.canonical = scope.canonical.model_copy(update=updates)
    with pytest.raises(CitableUnitError) as caught:
        CitableGenerationInputBuilder(scope.open).build(make_context(), make_retrieval_result())
    assert make_retrieval_result().sources[0].text not in str(caught.value)
    assert not scope.opened


@pytest.mark.parametrize(
    "failure", ["missing_source", "missing_block", "foreign_block", "range", "text"]
)
def test_missing_or_invalid_block_fails_closed(failure: str) -> None:
    scope = make_scope()
    assert scope.block is not None
    if failure == "missing_source":
        scope.canonical = None
    elif failure == "missing_block":
        scope.block = None
    else:
        updates: dict[str, dict[str, object]] = {
            "foreign_block": {"workspace_id": uuid4()},
            "range": {"start_character": 1},
            "text": {"text": "Wrong text."},
        }
        scope.block = scope.block.model_copy(update=updates[failure])
    with pytest.raises(CitableUnitError):
        CitableGenerationInputBuilder(scope.open).build(make_context(), make_retrieval_result())


def test_empty_selection_builds_empty_catalog_without_source_reads() -> None:
    scope = make_scope()
    result = make_retrieval_result().model_copy(update={"sources": (), "candidates": ()})
    request = CitableGenerationInputBuilder(scope.open).build(make_context(), result)
    assert not request.catalog.units and not request.sources
    assert scope.reads == 0


def test_foreign_request_is_rejected_before_opening_database() -> None:
    scope = make_scope()
    with pytest.raises(CitableUnitError):
        CitableGenerationInputBuilder(scope.open).build(
            WorkspaceContext(user_id=uuid4(), workspace_id=uuid4()), make_retrieval_result()
        )
    assert scope.reads == 0


def test_empty_catalog_skips_generation_and_both_accounting_layers() -> None:
    scope = make_scope()
    executor = Mock()
    answer_scope = RecordingAnswerValidationUnitOfWork()
    service, generator = make_service(
        answer_scope,
        make_answer_proposal(),
        provider_operation_executor=executor,
        generation_input_builder=CitableGenerationInputBuilder(scope.open),
    )
    result = service.generate(
        make_context(), make_retrieval_result().model_copy(update={"sources": (), "candidates": ()})
    )
    assert result.answer.state is AnswerState.INSUFFICIENT
    assert not generator.calls
    executor.execute.assert_not_called()


def test_metadata_preflight_rejects_before_canonical_budget_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = make_scope()
    executor = Mock()
    answer_scope = RecordingAnswerValidationUnitOfWork()
    service, generator = make_service(
        answer_scope,
        make_answer_proposal(),
        provider_operation_executor=executor,
        generation_input_builder=CitableGenerationInputBuilder(scope.open),
    )

    def reject(*args: object) -> None:
        assert not scope.opened and not answer_scope.is_open
        raise CitableUnitLimitError("source-unit metadata exceeds the input reservation")

    monkeypatch.setattr(generator, "preflight", reject, raising=False)
    with pytest.raises(CitableUnitLimitError):
        service.generate(make_context(), make_retrieval_result())
    assert not generator.calls
    executor.execute.assert_not_called()
