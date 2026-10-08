"""Explicit, accounted reranking outside canonical retrieval transactions."""

from dataclasses import dataclass
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    ProviderBudgetExceededError,
    ProviderCostPolicy,
    ProviderOperationAttempt,
    ProviderUsageExceededReservationError,
)
from investment_research.modules.operations.domain import (
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderUsage,
)
from investment_research.modules.research.errors import ResearchProviderUnavailableError
from investment_research.modules.search.ports import (
    Reranker,
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderError,
    SearchProviderTransientError,
)
from investment_research.modules.search.reranking import select_reranked_context
from investment_research.modules.search.reranking_contracts import (
    BGE_RERANK_CONFIG_VERSION,
    BGE_RERANK_MODEL,
    PreparedRrfReranking,
    RrfRerankingSelectionResult,
)


@dataclass(frozen=True, slots=True)
class RerankingAccountingProfile:
    """Caller-reviewed identity, price, and per-attempt usage; no implicit free allowance."""

    provider: str
    cost_policy: ProviderCostPolicy
    maximum_usage: ProviderUsage

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("reranking provider identity is required")
        if self.maximum_usage.input_units < 1 or self.maximum_usage.output_units != 0:
            raise ValueError("reranking requires a positive input-unit ceiling and zero output")


@dataclass(frozen=True, slots=True)
class AccountedRerankingService:
    """Execute one attempt only after explicit server-owned public/synthetic approval.

    Composition must scope this approval to the reviewed corpus. It is not a client flag or
    private-data permission. The executor must independently enforce its durable request quota.
    """

    reranker: Reranker
    executor: ExecuteProviderOperationService
    accounting: RerankingAccountingProfile
    public_synthetic_content_approved: bool

    def __post_init__(self) -> None:
        if self.public_synthetic_content_approved is not True:
            raise ValueError("reranking requires explicit public/synthetic content approval")

    def select(
        self,
        context: WorkspaceContext,
        research_request_id: UUID,
        prepared: PreparedRrfReranking,
    ) -> RrfRerankingSelectionResult:
        """Reserve, execute once, validate, and finalize; never retry or silently fall back."""

        request = prepared.request
        if request is None:
            return select_reranked_context(prepared=prepared, response=None)
        attempt = ProviderOperationAttempt(
            correlation_id=research_request_id,
            research_request_id=research_request_id,
            processing_run_id=None,
            operation=ProviderOperationKind.RERANKING,
            provider=self.accounting.provider,
            model=BGE_RERANK_MODEL,
            configuration_version=BGE_RERANK_CONFIG_VERSION,
            maximum_usage=self.accounting.maximum_usage,
            cost_policy=self.accounting.cost_policy,
        )
        try:
            return self.executor.execute(
                context,
                attempt,
                lambda: select_reranked_context(
                    prepared=prepared, response=self.reranker.rerank(request)
                ),
                self._usage,
                self._classify_error,
            )
        except (ProviderBudgetExceededError, ProviderUsageExceededReservationError) as error:
            raise ResearchProviderUnavailableError(research_request_id, retriable=False) from error
        except SearchProviderError as error:
            raise ResearchProviderUnavailableError(
                research_request_id, retriable=isinstance(error, SearchProviderTransientError)
            ) from error

    @staticmethod
    def _usage(result: RrfRerankingSelectionResult) -> ProviderUsage:
        if result.response is None:
            raise SearchProviderContractError("non-empty reranking requires provider usage")
        return result.response.usage

    @staticmethod
    def _classify_error(error: Exception) -> ProviderOperationErrorCode:
        if isinstance(error, SearchProviderContractError):
            return ProviderOperationErrorCode.CONTRACT
        if isinstance(error, SearchProviderConfigurationError):
            return ProviderOperationErrorCode.CONFIGURATION
        return ProviderOperationErrorCode.UNAVAILABLE
