"""Application-owned persistence contracts for research and answers."""

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.research.domain import Answer, AnswerStatement, ResearchRequest
from investment_research.modules.search.ports import LexicalSearch, RetrievalRepository


class ResearchRepository(Protocol):
    """Persist research records inside one trusted workspace transaction."""

    def add_request(self, context: WorkspaceContext, request: ResearchRequest) -> None:
        """Stage one immutable research request."""

    def get_request_by_id(
        self,
        context: WorkspaceContext,
        research_request_id: UUID,
    ) -> ResearchRequest | None:
        """Return a scoped request without disclosing inaccessible identifiers."""

    def add_answer(self, context: WorkspaceContext, answer: Answer) -> None:
        """Stage the first lifecycle snapshot of a versioned answer."""

    def get_answer_by_id(
        self,
        context: WorkspaceContext,
        answer_id: UUID,
    ) -> Answer | None:
        """Return a scoped answer without disclosing inaccessible identifiers."""

    def update_answer(self, context: WorkspaceContext, answer: Answer) -> bool:
        """Advance a scoped answer by exactly one permitted lifecycle transition."""

    def add_statements(
        self,
        context: WorkspaceContext,
        answer: Answer,
        statements: Sequence[AnswerStatement],
    ) -> None:
        """Stage ordered validated statements for a user-visible answer."""

    def list_statements(
        self,
        context: WorkspaceContext,
        answer_id: UUID,
    ) -> Sequence[AnswerStatement]:
        """Return validated statements in deterministic display order."""


class ResearchRetrievalUnitOfWork(Protocol):
    """Research and Search capabilities sharing one atomic database transaction."""

    @property
    def research(self) -> ResearchRepository:
        """Return the repository that stages the immutable research request."""

    @property
    def lexical_search(self) -> LexicalSearch:
        """Return the adapter that selects eligible canonical chunks."""

    @property
    def retrieval(self) -> RetrievalRepository:
        """Return the repository that stages the run and selected candidates."""


type ResearchRetrievalUnitOfWorkFactory = Callable[
    [],
    AbstractContextManager[ResearchRetrievalUnitOfWork],
]
