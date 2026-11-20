"""Bound one opt-in experiment using the normal canonical application services."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import investment_research
from investment_research.bootstrap import ApiContainer
from investment_research.composition_options import ApiCompositionOptions
from investment_research.modules.documents.contracts import DocumentListEntry, DocumentListQuery
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ProviderBudgetExceededError,
    ProviderOperationAttempt,
    ProviderOperationCompletion,
)
from investment_research.modules.operations.domain import ProviderOperationState
from investment_research.modules.search.contracts import DenseRetrievalHit, DenseRetrievalQuery
from investment_research.modules.search.ports import DenseRetriever
from scripts.evaluation_profiles import REPOSITORY_ROOT, ResolvedEvaluationProfile
from scripts.evaluation_usage import CaseUsage, UsageSummary, summarize_attempts
from scripts.portfolio_snapshots import ArchiveMode, RecordedSource


class LiveEvaluationError(ValueError):
    """A safe failure with no provider, source, or environment contents."""


class RunBudgetExceededError(ProviderBudgetExceededError):
    """Stop before the next call; keep all earlier reservations, including failed calls."""


@dataclass
class RunBudget:
    maximum_micro_usd: int
    maximum_calls: int
    calls: int = 0
    reserved_micro_usd: int = 0
    attempts: list[dict[str, object]] = field(default_factory=list)
    exhausted: bool = False
    _pending: list[tuple[ProviderOperationAttempt, int]] = field(default_factory=list, repr=False)
    _scope: tuple[str, ArchiveMode | None, int] | None = None
    _cases: dict[tuple[str, ArchiveMode | None, int], None] = field(default_factory=dict)

    def admit(self, attempt: ProviderOperationAttempt) -> None:
        cost = attempt.cost_policy.cost_for(attempt.maximum_usage)
        index = self._reserve(attempt.operation.value, cost)
        self.attempts[index].update(
            provider=attempt.provider,
            model=attempt.model,
            config=attempt.configuration_version,
        )
        self._pending.append((attempt, index))

    def completed(
        self, attempt: ProviderOperationAttempt, completion: ProviderOperationCompletion
    ) -> None:
        for position in range(len(self._pending) - 1, -1, -1):
            pending, index = self._pending[position]
            if pending is attempt:
                del self._pending[position]
                known = (
                    completion.state is ProviderOperationState.SUCCEEDED
                    and completion.input_units is not None
                    and completion.output_units is not None
                )
                tokens = known and attempt.provider == "openai"
                self.attempts[index].update(
                    state=completion.state.value,
                    usage_status="reported" if known else "unknown",
                    input_tokens=completion.input_units if tokens else None,
                    output_tokens=completion.output_units if tokens else None,
                    estimated_usage_cost_micro_usd=completion.actual_cost_micro_usd
                    if known
                    else None,
                )
                return
        raise ValueError("completion has no admitted evaluation attempt")

    def begin_case(self, case: str, repetition: int, mode: ArchiveMode | None) -> None:
        key = CaseUsage(
            case=case, repetition=repetition, archive_mode=mode, usage=UsageSummary()
        ).key
        if key in self._cases:
            raise ValueError("evaluation observation already started")
        self._scope = key
        self._cases[key] = None

    def usage(self) -> UsageSummary:
        return summarize_attempts(self.attempts)

    def case_usage(self) -> tuple[CaseUsage, ...]:
        return tuple(
            CaseUsage(
                case=key[0],
                archive_mode=key[1],
                repetition=key[2],
                usage=summarize_attempts(
                    [
                        item
                        for item in self.attempts
                        if (item.get("case"), item.get("archive_mode"), item.get("repetition"))
                        == key
                    ]
                ),
            )
            for key in self._cases
        )

    def current_usage(self) -> UsageSummary:
        return summarize_attempts(
            [
                item
                for item in self.attempts
                if (item.get("case"), item.get("archive_mode"), item.get("repetition"))
                == self._scope
            ]
        )

    def _reserve(self, role: str, cost: int) -> int:
        if (
            self.calls >= self.maximum_calls
            or self.reserved_micro_usd + cost > self.maximum_micro_usd
        ):
            self.exhausted = True
            raise RunBudgetExceededError("evaluation run budget exhausted")
        self.calls += 1
        self.reserved_micro_usd += cost
        entry: dict[str, object] = {
            "role": role,
            "reserved_micro_usd": cost,
            "provider": "unknown",
            "state": "ADMITTED",
            "usage_status": "unknown",
            "input_tokens": None,
            "output_tokens": None,
            "estimated_usage_cost_micro_usd": None,
        }
        if self._scope is not None:
            entry.update(
                case=self._scope[0], archive_mode=self._scope[1], repetition=self._scope[2]
            )
        self.attempts.append(entry)
        return len(self.attempts) - 1

    def wrap_dense(self, provider: DenseRetriever) -> DenseRetriever:
        return _CountedDenseRetriever(provider, self)


@dataclass(frozen=True)
class _CountedDenseRetriever:
    provider: DenseRetriever
    budget: RunBudget

    def retrieve(
        self, context: WorkspaceContext, query: DenseRetrievalQuery
    ) -> Sequence[DenseRetrievalHit]:
        # Database query charges depend on account usage; bound requests separately from USD.
        index = self.budget._reserve("pinecone_query", 0)
        entry = self.budget.attempts[index]
        entry.update(provider="pinecone", usage_status="not_applicable")
        try:
            result = self.provider.retrieve(context, query)
        except Exception:
            entry["state"] = "FAILED"
            raise
        entry["state"] = "SUCCEEDED"
        return result


def budget_for(resolved: ResolvedEvaluationProfile) -> RunBudget:
    budget = resolved.profile.budget
    return RunBudget(int(Decimal(budget.max_spend_usd) * 1_000_000), budget.max_provider_calls)


def composition_for(
    resolved: ResolvedEvaluationProfile, budget: RunBudget
) -> ApiCompositionOptions:
    retrieval = resolved.profile.retrieval
    return ApiCompositionOptions(
        lexical_strategy=retrieval.lexical_strategy,
        text_neighbors=retrieval.text_neighbors,
        page_expansion=retrieval.page_expansion,
        company_priority=retrieval.company_priority,
        admit_provider_attempt=budget.admit,
        observe_provider_completion=budget.completed,
        wrap_dense_retriever=budget.wrap_dense,
    )


def validate_live_profile(resolved: ResolvedEvaluationProfile) -> None:
    profile = resolved.profile
    if profile.mode == "offline_replay" or profile.inputs.snapshot:
        raise LiveEvaluationError("fresh execution requires a live mode and no input snapshot")
    discovery = tuple(case for case in resolved.cases if case.kind == "discovery")
    if any(case.id not in profile.archive.case_filters for case in discovery):
        raise LiveEvaluationError(
            "each discovery case requires an explicit archive.case_filters table"
        )
    if discovery and profile.retrieval.lexical_strategy != "websearch":
        raise LiveEvaluationError(
            "archive discovery currently requires the websearch lexical strategy"
        )
    if profile.reranking.strategy == "pinecone":
        raise LiveEvaluationError("Pinecone reranking is not supported by this collector")
    provider_backed = len(discovery) != len(resolved.cases) or any(
        mode != "keyword" for mode in profile.archive.modes
    )
    if provider_backed and (
        Decimal(profile.budget.max_spend_usd) <= 0 or profile.budget.max_provider_calls <= 0
    ):
        raise LiveEvaluationError("live research requires positive spend and call limits")


def inventory(container: ApiContainer, context: WorkspaceContext) -> tuple[DocumentListEntry, ...]:
    """Read current, undeleted documents through the same workspace-scoped catalog as the UI."""

    items: list[DocumentListEntry] = []
    query = DocumentListQuery(page_size=100)
    for _ in range(100):
        page = container.list_documents_service.list_documents(context, query)
        items.extend(page.items)
        if page.next_cursor is None:
            return tuple(sorted(items, key=lambda item: item.document_id))
        query = DocumentListQuery(page_size=100, after=page.next_cursor)
    raise LiveEvaluationError("evaluation corpus exceeds the bounded inventory")


def sources_for(
    resolved: ResolvedEvaluationProfile, items: tuple[DocumentListEntry, ...]
) -> tuple[RecordedSource, ...]:
    titles = [item.title for item in items]
    required = {alias for case in resolved.cases for alias in case.document_scope}
    if any(titles.count(resolved.catalog.sources[alias]) != 1 for alias in required):
        raise LiveEvaluationError("required catalog titles must resolve to one current document")
    if not items or any(
        item.processing is None or item.processing.state != "SEARCHABLE" for item in items
    ):
        raise LiveEvaluationError("freeze a nonempty, fully searchable corpus before execution")
    aliases = {title: alias for alias, title in resolved.catalog.sources.items()}
    sources = tuple(
        RecordedSource(
            alias=aliases.get(item.title, f"archive-{item.document_id.hex}"),
            document_id=item.document_id,
            document_version_id=item.document_version_id,
            title=item.title,
        )
        for item in items
    )
    if len({item.alias for item in sources}) != len(sources):
        raise LiveEvaluationError("catalog source titles must identify unique documents")
    return sources


def inventory_digest(items: tuple[DocumentListEntry, ...]) -> str:
    return hashlib.sha256(
        json.dumps([item.model_dump(mode="json") for item in items], sort_keys=True).encode()
    ).hexdigest()


def code_digest() -> str:
    """Hash Python sources from this installed app and the executing scripts, never env files."""

    digest = hashlib.sha256()
    for label, root in (
        ("app", Path(investment_research.__file__).parent),
        ("scripts", REPOSITORY_ROOT / "backend/scripts"),
    ):
        for path in sorted(root.rglob("*.py")):
            digest.update(f"{label}/{path.relative_to(root)}\n".encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()
