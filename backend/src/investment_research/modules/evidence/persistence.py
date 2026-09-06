"""SQLAlchemy mappings and scoped repository for evidence and validation."""

from collections.abc import Sequence
from datetime import datetime
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from investment_research.infrastructure.database.base import Base
from investment_research.modules.evidence.domain import (
    AnswerValidation,
    EvidenceRelationship,
    EvidenceRelationshipError,
    EvidenceScopeError,
    EvidenceSpan,
    StatementEvidence,
    ValidationRule,
)
from investment_research.modules.identity.domain import WorkspaceContext


class EvidenceSpanRow(Base):
    """Persisted exact source text anchored to one immutable document version."""

    __tablename__ = "evidence_spans"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint(
            "workspace_id",
            "document_version_id",
            "structure_block_id",
            "start_character",
            "end_character",
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "document_version_id"],
            ["document_versions.workspace_id", "document_versions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "document_version_id", "structure_block_id"],
            [
                "structure_blocks.workspace_id",
                "structure_blocks.document_version_id",
                "structure_blocks.id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("page_number > 0", name="positive_page_number"),
        CheckConstraint(
            "start_character >= 0 AND end_character > start_character",
            name="ordered_source_offsets",
        ),
        CheckConstraint("exact_text ~ '[^[:space:]]'", name="non_empty_exact_text"),
        CheckConstraint(
            "char_length(exact_text) = end_character - start_character",
            name="matching_text_length",
        ),
        CheckConstraint("text_sha256 ~ '^[a-f0-9]{64}$'", name="valid_text_sha256"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    structure_block_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_character: Mapped[int] = mapped_column(Integer, nullable=False)
    end_character: Mapped[int] = mapped_column(Integer, nullable=False)
    exact_text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @classmethod
    def from_domain(cls, span: EvidenceSpan) -> Self:
        """Translate an immutable evidence span into a pending row."""

        return cls(**span.model_dump())

    def to_domain(self) -> EvidenceSpan:
        """Translate a loaded row into a validated evidence span snapshot."""

        return EvidenceSpan(
            id=self.id,
            workspace_id=self.workspace_id,
            document_version_id=self.document_version_id,
            structure_block_id=self.structure_block_id,
            page_number=self.page_number,
            start_character=self.start_character,
            end_character=self.end_character,
            exact_text=self.exact_text,
            text_sha256=self.text_sha256,
            created_at=self.created_at,
        )


class StatementEvidenceRow(Base):
    """Persisted ordered relationship between a statement and exact source span."""

    __tablename__ = "statement_evidence"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint(
            "workspace_id",
            "answer_statement_id",
            "evidence_span_id",
            "relationship",
        ),
        UniqueConstraint("workspace_id", "answer_statement_id", "display_order"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "answer_statement_id"],
            ["answer_statements.workspace_id", "answer_statements.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "evidence_span_id"],
            ["evidence_spans.workspace_id", "evidence_spans.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("display_order >= 0", name="non_negative_display_order"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    answer_statement_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    evidence_span_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    relationship: Mapped[EvidenceRelationship] = mapped_column(
        SqlEnum(
            EvidenceRelationship,
            name="evidence_relationship",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        ),
        nullable=False,
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @classmethod
    def from_domain(cls, relationship: StatementEvidence) -> Self:
        """Translate an immutable statement relationship into a pending row."""

        return cls(**relationship.model_dump())

    def to_domain(self) -> StatementEvidence:
        """Translate a loaded row into a validated statement relationship snapshot."""

        return StatementEvidence(
            id=self.id,
            workspace_id=self.workspace_id,
            answer_statement_id=self.answer_statement_id,
            evidence_span_id=self.evidence_span_id,
            relationship=self.relationship,
            display_order=self.display_order,
            created_at=self.created_at,
        )


class AnswerValidationRow(Base):
    """Persisted result of one versioned server-side validation rule."""

    __tablename__ = "answer_validations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "answer_id"],
            ["answers.workspace_id", "answers.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "answer_id", "answer_statement_id"],
            [
                "answer_statements.workspace_id",
                "answer_statements.answer_id",
                "answer_statements.id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "passed IS TRUE OR (passed IS FALSE AND reason IS NOT NULL "
            "AND char_length(btrim(reason)) > 0)",
            name="failure_has_reason",
        ),
        CheckConstraint(
            "char_length(btrim(validation_config_version)) > 0",
            name="non_empty_validation_config_version",
        ),
        Index(
            "ux_answer_validations_answer_rule",
            "workspace_id",
            "answer_id",
            "rule",
            unique=True,
            postgresql_where=text("answer_statement_id IS NULL"),
        ),
        Index(
            "ux_answer_validations_statement_rule",
            "workspace_id",
            "answer_id",
            "answer_statement_id",
            "rule",
            unique=True,
            postgresql_where=text("answer_statement_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    answer_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    answer_statement_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    rule: Mapped[ValidationRule] = mapped_column(
        SqlEnum(
            ValidationRule,
            name="validation_rule",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=64,
        ),
        nullable=False,
    )
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(1_000))
    validation_config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @classmethod
    def from_domain(cls, validation: AnswerValidation) -> Self:
        """Translate an immutable validation result into a pending row."""

        return cls(**validation.model_dump())

    def to_domain(self) -> AnswerValidation:
        """Translate a loaded row into a validated answer rule result snapshot."""

        return AnswerValidation(
            id=self.id,
            workspace_id=self.workspace_id,
            answer_id=self.answer_id,
            answer_statement_id=self.answer_statement_id,
            rule=self.rule,
            passed=self.passed,
            reason=self.reason,
            validation_config_version=self.validation_config_version,
            validated_at=self.validated_at,
        )


class SqlAlchemyEvidenceRepository:
    """Evidence repository bound to one caller-owned SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_span(self, context: WorkspaceContext, span: EvidenceSpan) -> None:
        """Stage one exact source span inside the active workspace."""

        self._require_scope(context, span.workspace_id)
        self._session.add(EvidenceSpanRow.from_domain(span))

    def get_span_by_id(
        self,
        context: WorkspaceContext,
        evidence_span_id: UUID,
    ) -> EvidenceSpan | None:
        """Return an exact evidence span from the trusted workspace only."""

        row = self._session.scalar(
            select(EvidenceSpanRow).where(
                EvidenceSpanRow.workspace_id == context.workspace_id,
                EvidenceSpanRow.id == evidence_span_id,
            )
        )
        return None if row is None else row.to_domain()

    def add_statement_evidence(
        self,
        context: WorkspaceContext,
        answer_statement_id: UUID,
        relationships: Sequence[StatementEvidence],
    ) -> None:
        """Stage complete ordered evidence with at least one supporting span."""

        if not relationships:
            raise EvidenceRelationshipError("answer statement requires evidence")

        relationship_keys: set[tuple[UUID, EvidenceRelationship]] = set()
        for relationship in relationships:
            self._require_scope(context, relationship.workspace_id)
            if relationship.answer_statement_id != answer_statement_id:
                raise EvidenceRelationshipError(
                    "evidence relationship does not belong to the answer statement"
                )
            relationship_key = (relationship.evidence_span_id, relationship.relationship)
            if relationship_key in relationship_keys:
                raise EvidenceRelationshipError("evidence relationship is duplicated")
            relationship_keys.add(relationship_key)

        if sorted(item.display_order for item in relationships) != list(range(len(relationships))):
            raise EvidenceRelationshipError("evidence display order must be contiguous from zero")
        if not any(item.relationship is EvidenceRelationship.SUPPORTS for item in relationships):
            raise EvidenceRelationshipError("answer statement requires supporting evidence")

        self._session.add_all(
            StatementEvidenceRow.from_domain(relationship) for relationship in relationships
        )

    def list_statement_evidence(
        self,
        context: WorkspaceContext,
        answer_statement_id: UUID,
    ) -> Sequence[StatementEvidence]:
        """Return scoped evidence relationships in stable display order."""

        rows = self._session.scalars(
            select(StatementEvidenceRow)
            .where(
                StatementEvidenceRow.workspace_id == context.workspace_id,
                StatementEvidenceRow.answer_statement_id == answer_statement_id,
            )
            .order_by(StatementEvidenceRow.display_order, StatementEvidenceRow.id)
        )
        return tuple(row.to_domain() for row in rows)

    def add_answer_validations(
        self,
        context: WorkspaceContext,
        answer_id: UUID,
        validations: Sequence[AnswerValidation],
    ) -> None:
        """Stage one answer's non-duplicated validation result set."""

        if not validations:
            raise EvidenceRelationshipError("answer requires validation results")

        validation_keys: set[tuple[UUID | None, ValidationRule]] = set()
        config_versions: set[str] = set()
        for validation in validations:
            self._require_scope(context, validation.workspace_id)
            if validation.answer_id != answer_id:
                raise EvidenceRelationshipError("validation result does not belong to the answer")
            validation_key = (validation.answer_statement_id, validation.rule)
            if validation_key in validation_keys:
                raise EvidenceRelationshipError("validation rule result is duplicated")
            validation_keys.add(validation_key)
            config_versions.add(validation.validation_config_version)

        if len(config_versions) != 1:
            raise EvidenceRelationshipError(
                "one validation result set must use one configuration version"
            )

        self._session.add_all(
            AnswerValidationRow.from_domain(validation) for validation in validations
        )

    def list_answer_validations(
        self,
        context: WorkspaceContext,
        answer_id: UUID,
    ) -> Sequence[AnswerValidation]:
        """Return scoped validation records in stable subject and rule order."""

        rows = self._session.scalars(
            select(AnswerValidationRow)
            .where(
                AnswerValidationRow.workspace_id == context.workspace_id,
                AnswerValidationRow.answer_id == answer_id,
            )
            .order_by(
                AnswerValidationRow.answer_statement_id.asc().nulls_first(),
                AnswerValidationRow.rule,
                AnswerValidationRow.id,
            )
        )
        return tuple(row.to_domain() for row in rows)

    @staticmethod
    def _require_scope(context: WorkspaceContext, workspace_id: UUID) -> None:
        """Reject a cross-workspace write before accessing persistence state."""

        if workspace_id != context.workspace_id:
            raise EvidenceScopeError("evidence write is outside the active workspace")
