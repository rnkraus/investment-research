"""Explicit process-local retrieval overrides; never accept these from an HTTP client."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from investment_research.modules.operations.application import (
    ProviderOperationAttempt,
    ProviderOperationCompletion,
)
from investment_research.modules.search.ports import DenseRetriever


@dataclass(frozen=True, slots=True)
class ApiCompositionOptions:
    lexical_strategy: Literal["websearch", "weighted_or"] = "websearch"
    text_neighbors: bool = True
    page_expansion: bool = True
    company_priority: bool = True
    admit_provider_attempt: Callable[[ProviderOperationAttempt], None] | None = None
    wrap_dense_retriever: Callable[[DenseRetriever], DenseRetriever] | None = None
    observe_provider_completion: (
        Callable[[ProviderOperationAttempt, ProviderOperationCompletion], None] | None
    ) = None
