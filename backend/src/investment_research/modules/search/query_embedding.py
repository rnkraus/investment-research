"""Budgeted query embedding shared by research and archive discovery."""

from dataclasses import dataclass
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    ProviderCostPolicy,
    ProviderOperationAttempt,
)
from investment_research.modules.operations.domain import (
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderUsage,
)
from investment_research.modules.search.application import (
    EmbeddingResponseContractError,
    validate_embedding_response,
)
from investment_research.modules.search.contracts import (
    EmbeddingInput,
    EmbeddingPurpose,
    EmbeddingRequest,
    EmbeddingResult,
)
from investment_research.modules.search.ports import (
    EmbeddingProvider,
    SearchProviderConfigurationError,
    SearchProviderContractError,
)


@dataclass(frozen=True, slots=True)
class QueryEmbeddingAccountingProfile:
    """Provider identity and reviewed unit pricing for query embeddings."""

    provider: str
    model: str
    cost_policy: ProviderCostPolicy


@dataclass(frozen=True, slots=True)
class AccountedQueryEmbedding:
    """Reserve a workspace budget before calling the configured embedding adapter."""

    provider: EmbeddingProvider
    executor: ExecuteProviderOperationService
    accounting: QueryEmbeddingAccountingProfile

    def embed(
        self,
        context: WorkspaceContext,
        *,
        text: str,
        configuration_version: str,
        correlation_id: UUID,
        research_request_id: UUID | None,
    ) -> EmbeddingResult:
        request = EmbeddingRequest(
            inputs=(
                EmbeddingInput(input_id=correlation_id, text=text, purpose=EmbeddingPurpose.QUERY),
            ),
            embedding_config_version=configuration_version,
        )
        attempt = ProviderOperationAttempt(
            correlation_id=correlation_id,
            research_request_id=research_request_id,
            processing_run_id=None,
            operation=(
                ProviderOperationKind.ARCHIVE_QUERY_EMBEDDING
                if research_request_id is None
                else ProviderOperationKind.QUERY_EMBEDDING
            ),
            provider=self.accounting.provider,
            model=self.accounting.model,
            configuration_version=configuration_version,
            maximum_usage=ProviderUsage(input_units=len(text.encode("utf-8")), output_units=0),
            cost_policy=self.accounting.cost_policy,
        )
        return self.executor.execute(
            context,
            attempt,
            lambda: validate_embedding_response(request, self.provider.embed(request)),
            lambda result: result.usage,
            self._classify_error,
        )

    @staticmethod
    def _classify_error(error: Exception) -> ProviderOperationErrorCode:
        if isinstance(error, (SearchProviderContractError, EmbeddingResponseContractError)):
            return ProviderOperationErrorCode.CONTRACT
        if isinstance(error, SearchProviderConfigurationError):
            return ProviderOperationErrorCode.CONFIGURATION
        return ProviderOperationErrorCode.UNAVAILABLE
