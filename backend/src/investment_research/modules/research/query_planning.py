"""Bounded query planning and fusion of lexical search results."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from uuid import UUID

from pydantic import ValidationError

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
from investment_research.modules.search.application import normalize_lexical_query
from investment_research.modules.search.contracts import LexicalSearchHit, LexicalSearchQuery
from investment_research.modules.search.fusion_contracts import RRF_RANK_CONSTANT
from investment_research.modules.search.observation_dates import (
    preserve_observation_date,
    requested_observation_date,
)
from investment_research.modules.search.ports import LexicalSearch

QUERY_PLAN_VERSION = "openai-terra-lexical-decomposition-v1"
QUERY_PLAN_MAX_OUTPUT_TOKENS = 512

_QUOTED_TEXT = re.compile(r'"[^"\n]*"|“[^”\n]*”|`[^`\n]*`')
_SEARCH_GOAL = (
    r"(?:how|what|when|where|which|who|why|compare|describe|explain|identify|list|show)\b"
)
_FIRST_GOAL = re.compile(rf"^\s*{_SEARCH_GOAL}\s+\S", re.IGNORECASE)
_FOLLOW_UP_GOAL = re.compile(
    rf"(?:\?\s*|(?:,\s*|\s+)(?:and|also)\s+){_SEARCH_GOAL}(?:\s+\S|\?)",
    re.IGNORECASE,
)


def has_explicit_multiple_search_goals(question: str) -> bool:
    """Recognize separately phrased requests without interpreting quoted material."""

    visible_question = _QUOTED_TEXT.sub(" ", question)
    first = _FIRST_GOAL.match(visible_question)
    return first is not None and _FOLLOW_UP_GOAL.search(visible_question, first.end()) is not None


class QueryPlanningUnavailableError(RuntimeError):
    """The provider did not return a usable, accounted query plan."""


@dataclass(frozen=True, slots=True)
class QueryPlan:
    queries: tuple[str, ...]
    usage: ProviderUsage


@dataclass(frozen=True, slots=True)
class AccountedQueryPlanner:
    provider: Callable[[str], QueryPlan]
    executor: ExecuteProviderOperationService
    model: str
    cost_policy: ProviderCostPolicy

    def plan(self, context: WorkspaceContext, request_id: UUID, question: str) -> tuple[str, ...]:
        """Reserve the planning call and retain the original search if planning fails."""

        attempt = ProviderOperationAttempt(
            correlation_id=request_id,
            research_request_id=request_id,
            processing_run_id=None,
            operation=ProviderOperationKind.QUERY_PLANNING,
            provider="openai",
            model=self.model,
            configuration_version=QUERY_PLAN_VERSION,
            maximum_usage=ProviderUsage(
                input_units=2000 + len(question.encode("utf-8")),
                output_units=QUERY_PLAN_MAX_OUTPUT_TOKENS,
            ),
            cost_policy=self.cost_policy,
        )
        try:
            result = self.executor.execute(
                context,
                attempt,
                lambda: self.provider(question),
                lambda plan: plan.usage,
                lambda _: ProviderOperationErrorCode.UNAVAILABLE,
            )
        except (
            ProviderBudgetExceededError,
            ProviderUsageExceededReservationError,
            QueryPlanningUnavailableError,
        ):
            return ()
        if len(result.queries) > 2 or any(
            not query.strip() or len(query) > 100 for query in result.queries
        ):
            return ()
        try:
            original = normalize_lexical_query(question)
            observation_date = requested_observation_date(question)
            normalized = tuple(
                dict.fromkeys(
                    normalize_lexical_query(
                        preserve_observation_date(query, observation_date)
                        if observation_date is not None
                        else query
                    )
                    for query in result.queries
                )
            )
        except ValidationError:
            return ()
        return tuple(query for query in normalized if query != original and len(query) <= 100)


def search_lexical_queries(
    reader: LexicalSearch,
    context: WorkspaceContext,
    original: LexicalSearchQuery,
    additional_queries: tuple[str, ...],
) -> tuple[LexicalSearchHit, ...]:
    """Merge ranks within the lexical lane under its existing candidate limit."""

    original_hits = tuple(reader.search(context, original))
    if not additional_queries:
        return original_hits
    hits: dict[UUID, LexicalSearchHit] = {}
    scores: dict[UUID, Fraction] = {}
    rankings = [original_hits]
    for terms in additional_queries:
        query = original.model_copy(update={"normalized_query": terms})
        rankings.append(tuple(reader.search(context, query)))
    for ranking in rankings:
        for rank, hit in enumerate(ranking, 1):
            hits.setdefault(hit.chunk_id, hit)
            scores[hit.chunk_id] = scores.get(hit.chunk_id, Fraction(0)) + Fraction(
                1, RRF_RANK_CONSTANT + rank
            )
    ranked = sorted(hits, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    return tuple(hits[chunk_id] for chunk_id in ranked[: original.candidate_limit])
