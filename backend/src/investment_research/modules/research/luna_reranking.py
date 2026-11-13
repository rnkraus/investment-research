"""Budget and validate optional listwise reranking of canonical passages."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    ProviderBudgetExceededError,
    ProviderCostPolicy,
    ProviderOperationAttempt,
    ProviderRequestQuotaUnavailableError,
    ProviderUsageExceededReservationError,
)
from investment_research.modules.operations.domain import (
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderUsage,
)
from investment_research.modules.search.contracts import SelectedRetrievalCandidate

LUNA_RERANK_CONFIG_VERSION = "openai-luna-listwise-v1"
LUNA_RERANK_POOL_LIMIT = 24
LUNA_RERANK_MAX_OUTPUT_TOKENS = 512
LUNA_RERANK_MAX_INPUT_BYTES = 52_000


class LunaRerankingUnavailableError(RuntimeError):
    """A fixed failure when an optional ranking cannot be used."""


@dataclass(frozen=True, slots=True)
class ListwiseRanking:
    ids: tuple[int, ...]
    usage: ProviderUsage


@dataclass(frozen=True, slots=True)
class AccountedLunaReranker:
    provider: Callable[[str, int, int], ListwiseRanking]
    executor: ExecuteProviderOperationService
    model: str
    cost_policy: ProviderCostPolicy

    def select(
        self,
        context: WorkspaceContext,
        request_id: UUID,
        question: str,
        candidates: tuple[SelectedRetrievalCandidate, ...],
        *,
        chunk_limit: int,
        character_limit: int,
    ) -> tuple[SelectedRetrievalCandidate, ...] | None:
        """Return a bounded selection, or retain the app's original context on failure."""
        count = len(candidates)
        selection_size = min(count, chunk_limit)
        if not 1 < count <= LUNA_RERANK_POOL_LIMIT:
            return None
        payload = json.dumps(
            {
                "question": question,
                "passages": [
                    {"id": index, "text": candidate.text}
                    for index, candidate in enumerate(candidates, 1)
                ],
            },
            ensure_ascii=False,
        )
        size = len(payload.encode("utf-8"))
        if size > LUNA_RERANK_MAX_INPUT_BYTES:
            return None

        def rank() -> ListwiseRanking:
            result = self.provider(payload, count, selection_size)
            if (
                len(result.ids) != selection_size
                or len(set(result.ids)) != selection_size
                or any(not 1 <= rank <= count for rank in result.ids)
            ):
                raise LunaRerankingUnavailableError("invalid passage IDs")
            if sum(len(candidates[rank - 1].text) for rank in result.ids) > character_limit:
                raise LunaRerankingUnavailableError("selection exceeds the context limit")
            return result

        try:
            result = self.executor.execute(
                context,
                ProviderOperationAttempt(
                    correlation_id=request_id,
                    research_request_id=request_id,
                    processing_run_id=None,
                    operation=ProviderOperationKind.RERANKING,
                    provider="openai",
                    model=self.model,
                    configuration_version=LUNA_RERANK_CONFIG_VERSION,
                    maximum_usage=ProviderUsage(
                        input_units=size + 4_096, output_units=LUNA_RERANK_MAX_OUTPUT_TOKENS
                    ),
                    cost_policy=self.cost_policy,
                ),
                rank,
                lambda ranking: ranking.usage,
                lambda _: ProviderOperationErrorCode.UNAVAILABLE,
            )
        except (
            LunaRerankingUnavailableError,
            ProviderBudgetExceededError,
            ProviderRequestQuotaUnavailableError,
            ProviderUsageExceededReservationError,
        ):
            return None
        return tuple(
            candidates[rank - 1].model_copy(update={"selection_rank": position})
            for position, rank in enumerate(result.ids, 1)
        )
