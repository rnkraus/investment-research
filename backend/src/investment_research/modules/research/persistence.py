"""SQLAlchemy mappings and scoped repository for research and answers."""

from collections.abc import Sequence
from datetime import datetime
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from investment_research.infrastructure.database.base import Base
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.research.domain import (
    Answer,
    AnswerState,
    AnswerStatement,
    CoverageStatus,
    InvalidAnswerTransitionError,
    ResearchRelationshipError,
    ResearchRequest,
    ResearchScopeError,
    StatementSupportStatus,
    StatementType,
    TemporalMode,
)


class ResearchRequestRow(Base):
    """Persisted immutable question and effective archive context."""

    __tablename__ = "research_requests"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        CheckConstraint("question ~ '[^[:space:]]'", name="non_empty_question"),
        CheckConstraint(
            "(temporal_mode = 'CURRENT' AND cutoff_at IS NULL) OR "
            "(temporal_mode IN ('PUBLIC_KNOWLEDGE', 'PERSONAL_ARCHIVE') "
            "AND cutoff_at IS NOT NULL)",
            name="consistent_temporal_cutoff",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    document_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
    )
    temporal_mode: Mapped[TemporalMode] = mapped_column(
        SqlEnum(
            TemporalMode,
            name="temporal_mode",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        ),
        nullable=False,
    )
    cutoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @classmethod
    def from_domain(cls, request: ResearchRequest) -> Self:
        """Translate an immutable request into a pending database row."""

        return cls(
            id=request.id,
            workspace_id=request.workspace_id,
            question=request.question,
            document_ids=list(request.document_ids),
            temporal_mode=request.temporal_mode,
            cutoff_at=request.cutoff_at,
            created_at=request.created_at,
        )

    def to_domain(self) -> ResearchRequest:
        """Translate a loaded row into a validated request snapshot."""

        return ResearchRequest(
            id=self.id,
            workspace_id=self.workspace_id,
            question=self.question,
            document_ids=tuple(self.document_ids),
            temporal_mode=self.temporal_mode,
            cutoff_at=self.cutoff_at,
            created_at=self.created_at,
        )


class AnswerRow(Base):
    """Persisted lifecycle and reproducibility data for one answer version."""

    __tablename__ = "answers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "research_request_id", "id"),
        UniqueConstraint("workspace_id", "research_request_id", "version_number"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "research_request_id"],
            ["research_requests.workspace_id", "research_requests.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "research_request_id", "retrieval_run_id"],
            [
                "retrieval_runs.workspace_id",
                "retrieval_runs.research_request_id",
                "retrieval_runs.id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("version_number > 0", name="positive_version_number"),
        CheckConstraint(
            "char_length(btrim(generator_version)) > 0 "
            "AND char_length(btrim(generation_config_version)) > 0 "
            "AND char_length(btrim(prompt_version)) > 0",
            name="non_empty_configuration_versions",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ordered_completion_time",
        ),
        CheckConstraint(
            "(state IN ('REQUESTED', 'RETRIEVING') AND retrieval_run_id IS NULL) OR "
            "(state IN ('GENERATING', 'VALIDATING', 'STREAMED', 'INSUFFICIENT', "
            "'REJECTED', 'REGENERATED') AND retrieval_run_id IS NOT NULL)",
            name="consistent_retrieval_reference",
        ),
        CheckConstraint(
            "(state IN ('REQUESTED', 'RETRIEVING', 'GENERATING', 'VALIDATING') "
            "AND completed_at IS NULL AND coverage_status IS NULL AND rendered_text IS NULL) OR "
            "(state = 'STREAMED' AND completed_at IS NOT NULL "
            "AND coverage_status IN ('COMPLETE', 'PARTIAL', 'CONFLICTING') "
            "AND rendered_text ~ '[^[:space:]]') OR "
            "(state = 'INSUFFICIENT' AND completed_at IS NOT NULL "
            "AND coverage_status = 'INSUFFICIENT' "
            "AND rendered_text ~ '[^[:space:]]') OR "
            "(state IN ('REJECTED', 'REGENERATED') AND completed_at IS NOT NULL "
            "AND coverage_status IS NULL AND rendered_text IS NULL)",
            name="consistent_output_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    research_request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    retrieval_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[AnswerState] = mapped_column(
        SqlEnum(
            AnswerState,
            name="answer_state",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        ),
        nullable=False,
    )
    coverage_status: Mapped[CoverageStatus | None] = mapped_column(
        SqlEnum(
            CoverageStatus,
            name="coverage_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        )
    )
    generator_version: Mapped[str] = mapped_column(String(128), nullable=False)
    generation_config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    rendered_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @classmethod
    def from_domain(cls, answer: Answer) -> Self:
        """Translate an answer lifecycle snapshot into a pending row."""

        return cls(**answer.model_dump())

    def apply_lifecycle(self, answer: Answer) -> None:
        """Apply fields that may change during a permitted lifecycle transition."""

        self.retrieval_run_id = answer.retrieval_run_id
        self.state = answer.state
        self.coverage_status = answer.coverage_status
        self.rendered_text = answer.rendered_text
        self.completed_at = answer.completed_at

    def to_domain(self) -> Answer:
        """Translate a loaded row into a validated answer snapshot."""

        return Answer(
            id=self.id,
            workspace_id=self.workspace_id,
            research_request_id=self.research_request_id,
            retrieval_run_id=self.retrieval_run_id,
            version_number=self.version_number,
            state=self.state,
            coverage_status=self.coverage_status,
            generator_version=self.generator_version,
            generation_config_version=self.generation_config_version,
            prompt_version=self.prompt_version,
            rendered_text=self.rendered_text,
            created_at=self.created_at,
            completed_at=self.completed_at,
        )


class AnswerStatementRow(Base):
    """Persisted validated archive-derived statement in display order."""

    __tablename__ = "answer_statements"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "answer_id", "id"),
        UniqueConstraint("workspace_id", "answer_id", "ordinal"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "answer_id"],
            ["answers.workspace_id", "answers.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="non_negative_ordinal"),
        CheckConstraint("text ~ '[^[:space:]]'", name="non_empty_text"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    answer_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    statement_type: Mapped[StatementType] = mapped_column(
        SqlEnum(
            StatementType,
            name="statement_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        ),
        nullable=False,
    )
    support_status: Mapped[StatementSupportStatus] = mapped_column(
        SqlEnum(
            StatementSupportStatus,
            name="statement_support_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @classmethod
    def from_domain(cls, statement: AnswerStatement) -> Self:
        """Translate an immutable validated statement into a pending row."""

        return cls(**statement.model_dump())

    def to_domain(self) -> AnswerStatement:
        """Translate a loaded row into a validated statement snapshot."""

        return AnswerStatement(
            id=self.id,
            workspace_id=self.workspace_id,
            answer_id=self.answer_id,
            ordinal=self.ordinal,
            text=self.text,
            statement_type=self.statement_type,
            support_status=self.support_status,
            created_at=self.created_at,
        )


class SqlAlchemyResearchRepository:
    """Research repository bound to one caller-owned SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_request(self, context: WorkspaceContext, request: ResearchRequest) -> None:
        """Stage one immutable request inside the active workspace."""

        self._require_scope(context, request.workspace_id)
        self._session.add(ResearchRequestRow.from_domain(request))

    def get_request_by_id(
        self,
        context: WorkspaceContext,
        research_request_id: UUID,
    ) -> ResearchRequest | None:
        """Return a research request from the trusted workspace only."""

        row = self._session.scalar(
            select(ResearchRequestRow).where(
                ResearchRequestRow.workspace_id == context.workspace_id,
                ResearchRequestRow.id == research_request_id,
            )
        )
        return None if row is None else row.to_domain()

    def add_answer(self, context: WorkspaceContext, answer: Answer) -> None:
        """Stage a newly requested answer before retrieval begins."""

        self._require_scope(context, answer.workspace_id)
        if answer.state is not AnswerState.REQUESTED:
            raise ResearchRelationshipError("new answer must start in REQUESTED state")
        self._session.add(AnswerRow.from_domain(answer))

    def get_answer_by_id(
        self,
        context: WorkspaceContext,
        answer_id: UUID,
    ) -> Answer | None:
        """Return an answer from the trusted workspace only."""

        row = self._session.scalar(
            select(AnswerRow).where(
                AnswerRow.workspace_id == context.workspace_id,
                AnswerRow.id == answer_id,
            )
        )
        return None if row is None else row.to_domain()

    def update_answer(self, context: WorkspaceContext, answer: Answer) -> bool:
        """Advance lifecycle fields without changing answer identity or configuration."""

        self._require_scope(context, answer.workspace_id)
        row = self._session.scalar(
            select(AnswerRow).where(
                AnswerRow.workspace_id == context.workspace_id,
                AnswerRow.id == answer.id,
            )
        )
        if row is None:
            return False
        if (
            row.research_request_id != answer.research_request_id
            or row.version_number != answer.version_number
            or row.generator_version != answer.generator_version
            or row.generation_config_version != answer.generation_config_version
            or row.prompt_version != answer.prompt_version
            or row.created_at != answer.created_at
        ):
            raise ResearchRelationshipError("answer identity or configuration changed")
        if row.retrieval_run_id is not None and row.retrieval_run_id != answer.retrieval_run_id:
            raise ResearchRelationshipError("answer retrieval run changed")
        if not row.state.can_transition_to(answer.state):
            raise InvalidAnswerTransitionError(
                f"answer cannot transition from {row.state} to {answer.state}"
            )

        row.apply_lifecycle(answer)
        return True

    def add_statements(
        self,
        context: WorkspaceContext,
        answer: Answer,
        statements: Sequence[AnswerStatement],
    ) -> None:
        """Stage complete ordered statements only for a streamed answer."""

        self._require_scope(context, answer.workspace_id)
        if answer.state is not AnswerState.STREAMED:
            raise ResearchRelationshipError("statements require a STREAMED answer")
        if not statements:
            raise ResearchRelationshipError("STREAMED answer requires statements")

        for statement in statements:
            self._require_scope(context, statement.workspace_id)
            if statement.answer_id != answer.id:
                raise ResearchRelationshipError("statement does not belong to the answer")
        if sorted(statement.ordinal for statement in statements) != list(range(len(statements))):
            raise ResearchRelationshipError("statement ordinals must be contiguous from zero")

        self._session.add_all(AnswerStatementRow.from_domain(statement) for statement in statements)

    def list_statements(
        self,
        context: WorkspaceContext,
        answer_id: UUID,
    ) -> Sequence[AnswerStatement]:
        """Return validated statements for a known scoped answer in display order."""

        answer_exists = self._session.scalar(
            select(AnswerRow.id).where(
                AnswerRow.workspace_id == context.workspace_id,
                AnswerRow.id == answer_id,
            )
        )
        if answer_exists is None:
            return ()

        rows = self._session.scalars(
            select(AnswerStatementRow)
            .where(
                AnswerStatementRow.workspace_id == context.workspace_id,
                AnswerStatementRow.answer_id == answer_id,
            )
            .order_by(AnswerStatementRow.ordinal, AnswerStatementRow.id)
        )
        return tuple(row.to_domain() for row in rows)

    @staticmethod
    def _require_scope(context: WorkspaceContext, workspace_id: UUID) -> None:
        """Reject a cross-workspace write before accessing persistence state."""

        if workspace_id != context.workspace_id:
            raise ResearchScopeError("research write is outside the active workspace")
