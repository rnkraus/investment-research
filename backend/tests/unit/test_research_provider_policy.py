"""Unit tests for safe research-provider failure policy."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from unittest.mock import Mock, sentinel
from uuid import UUID

import pytest

from investment_research.modules.evidence.ports import (
    StatementSupportProviderContractError,
    StatementSupportProviderTransientError,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ProviderBudgetExceededError,
    ProviderUsageCostExceededReservationError,
)
from investment_research.modules.research.application import (
    AnswerResearchRequestService,
    GenerateValidatedAnswerService,
    ResearchProviderUnavailableError,
    RetrieveResearchService,
)
from investment_research.modules.research.contracts import (
    ResearchModel,
    ResearchRetrievalInput,
    ResearchRetrievalResult,
    ValidatedAnswerResult,
)
from investment_research.modules.research.domain import ResearchRequest, TemporalMode
from investment_research.modules.research.ports import (
    AnswerGenerationProviderConfigurationError,
    AnswerGenerationProviderTransientError,
)
from investment_research.modules.search.domain import (
    LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
    RetrievalFilters,
    RetrievalMode,
    RetrievalRun,
)

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
REQUEST_ID = UUID("10000000-0000-4000-8000-000000000001")
RUN_ID = UUID("10000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)
QUESTION = "What constrained new customer deployments?"


def make_context() -> WorkspaceContext:
    """Return the trusted local identity for the policy tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_retrieval_result() -> ResearchRetrievalResult:
    """Return one already-persisted empty retrieval result."""

    request = ResearchRequest(
        id=REQUEST_ID,
        workspace_id=WORKSPACE_ID,
        question=QUESTION,
        temporal_mode=TemporalMode.CURRENT,
        created_at=NOW,
    )
    run = RetrievalRun(
        id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        research_request_id=REQUEST_ID,
        query_text=QUESTION,
        normalized_query="constrained new customer deployments",
        filters=RetrievalFilters(),
        mode=RetrievalMode.LEXICAL,
        lexical_config_version="m1-english-lexical-v1",
        selector_config_version=LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
        lexical_candidate_limit=10,
        started_at=NOW,
        completed_at=NOW,
    )
    return ResearchRetrievalResult(
        request=request,
        retrieval_run=run,
        candidates=(),
        sources=(),
    )


@dataclass(slots=True)
class StaticRetrievalService:
    """Return one persisted retrieval result and record calls."""

    result: ResearchRetrievalResult = field(default_factory=make_retrieval_result)
    calls: list[tuple[WorkspaceContext, ResearchRetrievalInput]] = field(default_factory=list)

    def retrieve(
        self,
        context: WorkspaceContext,
        retrieval_input: ResearchRetrievalInput,
    ) -> ResearchRetrievalResult:
        self.calls.append((context, retrieval_input))
        return self.result


@dataclass(slots=True)
class FailingGenerationService:
    """Raise one configured failure and prove the policy does not retry."""

    error: BaseException
    calls: list[tuple[WorkspaceContext, ResearchRetrievalResult]] = field(default_factory=list)

    def generate(
        self,
        context: WorkspaceContext,
        retrieval_result: ResearchRetrievalResult,
    ) -> ValidatedAnswerResult:
        self.calls.append((context, retrieval_result))
        raise self.error


@pytest.mark.parametrize(
    ("provider_error", "retriable"),
    [
        pytest.param(
            AnswerGenerationProviderTransientError("safe transient generation failure"),
            True,
            id="generation-transient",
        ),
        pytest.param(
            AnswerGenerationProviderConfigurationError("safe generation configuration failure"),
            False,
            id="generation-permanent",
        ),
        pytest.param(
            StatementSupportProviderTransientError("safe transient support failure"),
            True,
            id="support-transient",
        ),
        pytest.param(
            StatementSupportProviderContractError("safe support contract failure"),
            False,
            id="support-permanent",
        ),
        pytest.param(
            ProviderBudgetExceededError("monthly provider budget exceeded"),
            False,
            id="budget-exceeded",
        ),
        pytest.param(
            ProviderUsageCostExceededReservationError(
                "provider usage cost exceeded its reservation"
            ),
            False,
            id="usage-exceeded-reservation",
        ),
    ],
)
def test_provider_failure_is_mapped_once_with_the_persisted_request_id(
    provider_error: Exception,
    retriable: bool,
) -> None:
    retrieval = StaticRetrievalService()
    generation = FailingGenerationService(provider_error)
    service = AnswerResearchRequestService(
        retrieval_service=cast(RetrieveResearchService, retrieval),
        generation_service=cast(GenerateValidatedAnswerService, generation),
    )
    retrieval_input = ResearchRetrievalInput(question=QUESTION)

    with pytest.raises(ResearchProviderUnavailableError) as captured:
        service.answer(make_context(), retrieval_input)

    assert captured.value.research_request_id == REQUEST_ID
    assert captured.value.retriable is retriable
    assert captured.value.__cause__ is provider_error
    assert retrieval.calls == [(make_context(), retrieval_input)]
    assert generation.calls == [(make_context(), retrieval.result)]


def test_non_provider_failure_is_not_reclassified() -> None:
    application_error = RuntimeError("unrelated application failure")
    generation = FailingGenerationService(application_error)
    service = AnswerResearchRequestService(
        retrieval_service=cast(RetrieveResearchService, StaticRetrievalService()),
        generation_service=cast(GenerateValidatedAnswerService, generation),
    )

    with pytest.raises(RuntimeError) as captured:
        service.answer(make_context(), ResearchRetrievalInput(question=QUESTION))

    assert captured.value is application_error
    assert len(generation.calls) == 1


def test_selected_model_pair_uses_its_own_generation_service() -> None:
    retrieval = StaticRetrievalService()
    default_generation = Mock(spec=GenerateValidatedAnswerService)
    selected_generation = Mock(spec=GenerateValidatedAnswerService)
    selected_generation.generate.return_value = sentinel.validated_result
    service = AnswerResearchRequestService(
        retrieval_service=cast(RetrieveResearchService, retrieval),
        generation_service=default_generation,
        model_services={(ResearchModel.LUNA, ResearchModel.TERRA): selected_generation},
    )
    retrieval_input = ResearchRetrievalInput(
        question=QUESTION,
        generation_model=ResearchModel.LUNA,
        support_model=ResearchModel.TERRA,
    )

    result = service.answer(make_context(), retrieval_input)

    assert result is sentinel.validated_result
    selected_generation.generate.assert_called_once_with(make_context(), retrieval.result)
    default_generation.generate.assert_not_called()


def test_unavailable_model_pair_fails_before_retrieval() -> None:
    retrieval = StaticRetrievalService()
    service = AnswerResearchRequestService(
        retrieval_service=cast(RetrieveResearchService, retrieval),
        generation_service=Mock(spec=GenerateValidatedAnswerService),
    )

    with pytest.raises(ValueError, match="selected research models are unavailable"):
        service.answer(
            make_context(),
            ResearchRetrievalInput(question=QUESTION, support_model=ResearchModel.LUNA),
        )

    assert retrieval.calls == []
