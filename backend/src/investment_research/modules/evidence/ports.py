"""Application-owned persistence contracts for evidence and validation."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from investment_research.modules.evidence.domain import (
    AnswerValidation,
    EvidenceSpan,
    StatementEvidence,
)
from investment_research.modules.identity.domain import WorkspaceContext


class EvidenceRepository(Protocol):
    """Persist exact evidence and validation records in one trusted workspace."""

    def add_span(self, context: WorkspaceContext, span: EvidenceSpan) -> None:
        """Stage one exact immutable source span."""

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
