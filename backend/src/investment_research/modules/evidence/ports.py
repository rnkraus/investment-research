"""Application-owned persistence and provider contracts for evidence validation."""

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable
from uuid import UUID

from investment_research.modules.evidence.contracts import (
    CanonicalEvidenceSource,
    CanonicalEvidenceSpanSource,
    ContextualStatementSupportInput,
    StatementSupportResult,
    ValidatedEvidenceReference,
)
from investment_research.modules.evidence.domain import (
    AnswerValidation,
    EvidenceSpan,
    StatementEvidence,
)
from investment_research.modules.identity.domain import WorkspaceContext


class CanonicalEvidenceSourceReader(Protocol):
    """Reload one proposed candidate through the canonical workspace boundary."""

    def get_source(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
        retrieval_candidate_id: UUID,
    ) -> CanonicalEvidenceSource | None:
        """Return matching canonical state without disclosing inaccessible identifiers."""


class CanonicalEvidenceSpanReader(Protocol):
    """Load the canonical relationships needed to authorize one citation span."""

    def get_span_source(
        self,
        context: WorkspaceContext,
        evidence_span_id: UUID,
    ) -> CanonicalEvidenceSpanSource | None:
        """Return scoped canonical state without disclosing unavailable identifiers."""


class StatementSupportProviderError(RuntimeError):
    """Base class for safe statement-support provider failures."""


class StatementSupportProviderTransientError(StatementSupportProviderError):
    """The support provider is temporarily unavailable and may be retried externally."""


class StatementSupportProviderConfigurationError(StatementSupportProviderError):
    """The support request or provider configuration was rejected."""


class StatementSupportProviderContractError(StatementSupportProviderError):
    """The support provider returned an invalid or unexpected response."""


class StatementSupportProviderRefusalError(StatementSupportProviderError):
    """The support provider explicitly refused to evaluate the statement."""


class StatementSupportEvaluator(Protocol):
    """Evaluate whether exact canonical source text supports one generated statement."""

    def evaluate(self, statement: str, evidence_texts: Sequence[str]) -> StatementSupportResult:
        """Return one typed support decision and content-free provider usage."""


@runtime_checkable
class ContextualStatementSupportEvaluator(StatementSupportEvaluator, Protocol):
    """Opt-in support evaluation with source-separated original attribution context."""

    def evaluate_contextual(
        self, request: ContextualStatementSupportInput
    ) -> StatementSupportResult:
        """Evaluate a server-prepared input without authorizing additional factual evidence."""


class StatementSupportContextBuilder(Protocol):
    """Prepare original context from the current canonical validation snapshot."""

    def __call__(
        self,
        *,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
        statement: str,
        references: Sequence[ValidatedEvidenceReference],
        sources: Mapping[UUID, CanonicalEvidenceSource | None],
    ) -> ContextualStatementSupportInput | None:
        """Return only eligible source pairs, or no input when any reference is invalid."""


class EvidenceRepository(Protocol):
    """Persist exact evidence and validation records in one trusted workspace."""

    def add_span(self, context: WorkspaceContext, span: EvidenceSpan) -> None:
        """Stage one exact immutable source span."""

    def get_or_add_span(
        self,
        context: WorkspaceContext,
        span: EvidenceSpan,
    ) -> EvidenceSpan:
        """Return the canonical matching span, inserting it when it does not exist."""

    def get_span_by_id(
        self,
        context: WorkspaceContext,
        evidence_span_id: UUID,
    ) -> EvidenceSpan | None:
        """Return a scoped span without disclosing inaccessible identifiers."""

    def add_statement_evidence(
        self,
        context: WorkspaceContext,
        answer_statement_id: UUID,
        relationships: Sequence[StatementEvidence],
    ) -> None:
        """Stage a statement's complete ordered evidence relationships."""

    def list_statement_evidence(
        self,
        context: WorkspaceContext,
        answer_statement_id: UUID,
    ) -> Sequence[StatementEvidence]:
        """Return a scoped statement's evidence in deterministic display order."""

    def add_answer_validations(
        self,
        context: WorkspaceContext,
        answer_id: UUID,
        validations: Sequence[AnswerValidation],
    ) -> None:
        """Stage one answer's complete set of versioned rule results."""

    def list_answer_validations(
        self,
        context: WorkspaceContext,
        answer_id: UUID,
    ) -> Sequence[AnswerValidation]:
        """Return a scoped answer's validation results in stable rule order."""


class EvidenceReadUnitOfWork(Protocol):
    """Canonical citation reader scoped to one non-writing application operation."""

    @property
    def evidence_spans(self) -> CanonicalEvidenceSpanReader:
        """Return the reader used to authorize an exact evidence span."""


type EvidenceReadUnitOfWorkFactory = Callable[
    [],
    AbstractContextManager[EvidenceReadUnitOfWork],
]
