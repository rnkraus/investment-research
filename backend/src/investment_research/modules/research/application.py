"""Validated answer services and stable research application imports."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import UUID

from investment_research.modules.evidence.ports import (
    StatementSupportProviderError,
    StatementSupportProviderTransientError,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ProviderBudgetExceededError,
    ProviderUsageCostExceededReservationError,
)
from investment_research.modules.research.answer_validation import (
    AnswerGenerationAccountingProfile as AnswerGenerationAccountingProfile,
)
from investment_research.modules.research.answer_validation import (
    GenerateValidatedAnswerService as GenerateValidatedAnswerService,
)
from investment_research.modules.research.answer_validation import (
    StatementSupportAccountingProfile as StatementSupportAccountingProfile,
)
from investment_research.modules.research.contracts import (
    ResearchModel,
    ResearchProgressStage,
    ResearchRetrievalInput,
    ValidatedAnswerResult,
)
from investment_research.modules.research.errors import (
    ResearchProviderUnavailableError as ResearchProviderUnavailableError,
)
from investment_research.modules.research.ports import (
    AnswerGenerationProviderError,
    AnswerGenerationProviderTransientError,
)
from investment_research.modules.research.retrieval import (
    M1QueryRetrievalDependencies as M1QueryRetrievalDependencies,
)
from investment_research.modules.research.retrieval import (
    QueryEmbeddingAccountingProfile as QueryEmbeddingAccountingProfile,
)
from investment_research.modules.research.retrieval import (
    RetrieveResearchService as RetrieveResearchService,
)
from investment_research.modules.research.retrieval import (
    build_effective_retrieval_filters as build_effective_retrieval_filters,
)


@dataclass(frozen=True, slots=True)
class AnswerResearchRequestService:
    """Run retrieval and validated answer persistence as one application use case."""

    retrieval_service: RetrieveResearchService
    generation_service: GenerateValidatedAnswerService
    model_services: (
        Mapping[tuple[ResearchModel, ResearchModel], GenerateValidatedAnswerService] | None
    ) = None

    def answer(
        self,
        context: WorkspaceContext,
        retrieval_input: ResearchRetrievalInput,
        *,
        progress: Callable[[UUID, ResearchProgressStage], None] | None = None,
    ) -> ValidatedAnswerResult:
        """Return only the validated terminal result of one research request."""

        selected_models = (retrieval_input.generation_model, retrieval_input.support_model)
        if self.model_services is None:
            if selected_models != (ResearchModel.TERRA, ResearchModel.TERRA):
                raise ValueError("selected research models are unavailable in this runtime")
            generation_service = self.generation_service
        else:
            selected_service = self.model_services.get(selected_models)
            if selected_service is None:
                raise ValueError("selected research models are unavailable in this runtime")
            generation_service = selected_service

        if progress is None:
            retrieval_result = self.retrieval_service.retrieve(context, retrieval_input)
        else:
            retrieval_result = self.retrieval_service.retrieve(
                context, retrieval_input, progress=progress
            )
        try:
            if progress is None:
                return generation_service.generate(context, retrieval_result)
            return generation_service.generate(context, retrieval_result, progress=progress)
        except (
            AnswerGenerationProviderError,
            StatementSupportProviderError,
            ProviderBudgetExceededError,
            ProviderUsageCostExceededReservationError,
        ) as error:
            retriable = isinstance(
                error,
                (
                    AnswerGenerationProviderTransientError,
                    StatementSupportProviderTransientError,
                ),
            )
            raise ResearchProviderUnavailableError(
                retrieval_result.request.id,
                retriable=retriable,
            ) from error
