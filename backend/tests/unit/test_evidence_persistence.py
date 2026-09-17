"""Unit tests for exact evidence, validation records, and scoped persistence."""

from datetime import UTC, datetime
from hashlib import sha256
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import ForeignKeyConstraint, Table
from sqlalchemy.orm import Session

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.evidence.domain import (
    AnswerValidation,
    EvidenceRelationship,
    EvidenceRelationshipError,
    EvidenceScopeError,
    EvidenceSpan,
    StatementEvidence,
    ValidationRule,
)
from investment_research.modules.evidence.persistence import (
    AnswerValidationRow,
    EvidenceSpanRow,
    SqlAlchemyEvidenceRepository,
    StatementEvidenceRow,
)
from investment_research.modules.evidence.ports import EvidenceRepository
from investment_research.modules.identity.domain import WorkspaceContext

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
OTHER_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
DOCUMENT_VERSION_ID = UUID("50000000-0000-4000-8000-000000000001")
STRUCTURE_BLOCK_ID = UUID("50000000-0000-4000-8000-000000000002")
EVIDENCE_SPAN_ID = UUID("50000000-0000-4000-8000-000000000003")
OTHER_SPAN_ID = UUID("50000000-0000-4000-8000-000000000004")
ANSWER_ID = UUID("50000000-0000-4000-8000-000000000005")
ANSWER_STATEMENT_ID = UUID("50000000-0000-4000-8000-000000000006")
OTHER_STATEMENT_ID = UUID("50000000-0000-4000-8000-000000000007")
RELATIONSHIP_ID = UUID("50000000-0000-4000-8000-000000000008")
OTHER_RELATIONSHIP_ID = UUID("50000000-0000-4000-8000-000000000009")
VALIDATION_ID = UUID("50000000-0000-4000-8000-000000000010")
OTHER_VALIDATION_ID = UUID("50000000-0000-4000-8000-000000000011")
CREATED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
EXACT_TEXT = "FY2025 revenue was $184.2 million."
START_CHARACTER = 14


def make_context() -> WorkspaceContext:
    """Build the deterministic trusted context used by evidence tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_span(*, workspace_id: UUID = WORKSPACE_ID) -> EvidenceSpan:
    """Build one exact source span with its matching UTF-8 hash."""

    return EvidenceSpan(
        id=EVIDENCE_SPAN_ID,
        workspace_id=workspace_id,
        document_version_id=DOCUMENT_VERSION_ID,
        structure_block_id=STRUCTURE_BLOCK_ID,
        source_locator_kind=SourceLocatorKind.PDF_PAGE,
        page_number=2,
        source_start_character=None,
        source_end_character=None,
        start_character=START_CHARACTER,
        end_character=START_CHARACTER + len(EXACT_TEXT),
        exact_text=EXACT_TEXT,
        text_sha256=sha256(EXACT_TEXT.encode("utf-8")).hexdigest(),
        created_at=CREATED_AT,
    )


def make_relationship(
    *,
    relationship_id: UUID = RELATIONSHIP_ID,
    workspace_id: UUID = WORKSPACE_ID,
    answer_statement_id: UUID = ANSWER_STATEMENT_ID,
    evidence_span_id: UUID = EVIDENCE_SPAN_ID,
    relationship: EvidenceRelationship = EvidenceRelationship.SUPPORTS,
    display_order: int = 0,
) -> StatementEvidence:
    """Build one deterministic statement-to-source relationship."""

    return StatementEvidence(
        id=relationship_id,
        workspace_id=workspace_id,
        answer_statement_id=answer_statement_id,
        evidence_span_id=evidence_span_id,
        relationship=relationship,
        display_order=display_order,
        created_at=CREATED_AT,
    )


def make_validation(
    *,
    validation_id: UUID = VALIDATION_ID,
    workspace_id: UUID = WORKSPACE_ID,
    answer_id: UUID = ANSWER_ID,
    answer_statement_id: UUID | None = None,
    rule: ValidationRule = ValidationRule.COVERAGE,
    passed: bool = True,
    reason: str | None = None,
    config_version: str = "evidence-validation-v1",
) -> AnswerValidation:
    """Build one deterministic answer- or statement-level rule result."""

    return AnswerValidation(
        id=validation_id,
        workspace_id=workspace_id,
        answer_id=answer_id,
        answer_statement_id=answer_statement_id,
        rule=rule,
        passed=passed,
        reason=reason,
        validation_config_version=config_version,
        validated_at=CREATED_AT,
    )


def test_evidence_span_round_trips_exact_text_and_hash() -> None:
    span = make_span()

    row = EvidenceSpanRow.from_domain(span)

    assert row.to_domain() == span
    assert span.text_sha256 == sha256(span.exact_text.encode("utf-8")).hexdigest()


def test_evidence_span_round_trips_text_passage_locator() -> None:
    span = make_span()
    source_start = 41
    text_span = EvidenceSpan.model_validate(
        {
            **span.model_dump(),
            "source_locator_kind": SourceLocatorKind.TEXT_PASSAGE,
            "page_number": None,
            "source_start_character": source_start,
            "source_end_character": source_start + len(EXACT_TEXT),
        }
    )

    row = EvidenceSpanRow.from_domain(text_span)

    assert row.to_domain() == text_span
    assert text_span.page_number is None
    assert text_span.source_end_character == source_start + len(EXACT_TEXT)


@pytest.mark.parametrize(
    ("changes", "expected_error"),
    [
        pytest.param(
            {"end_character": START_CHARACTER},
            "greater than start_character",
            id="unordered-offsets",
        ),
        pytest.param(
            {"end_character": START_CHARACTER + len(EXACT_TEXT) + 1},
            "must match exact_text length",
            id="wrong-length",
        ),
        pytest.param(
            {"text_sha256": "0" * 64},
            "must match exact_text",
            id="wrong-hash",
        ),
        pytest.param(
            {
                "exact_text": " \n\t ",
                "end_character": START_CHARACTER + 4,
                "text_sha256": sha256(b" \n\t ").hexdigest(),
            },
            "non-whitespace",
            id="empty-text",
        ),
    ],
)
def test_evidence_span_rejects_inexact_source_identity(
    changes: dict[str, object],
    expected_error: str,
) -> None:
    payload = {**make_span().model_dump(), **changes}

    with pytest.raises(ValidationError, match=expected_error):
        EvidenceSpan.model_validate(payload)


@pytest.mark.parametrize(
    ("changes", "expected_error"),
    [
        pytest.param(
            {"source_start_character": 10, "source_end_character": 20},
            "PDF_PAGE evidence must not define text-passage offsets",
            id="pdf-with-text-offsets",
        ),
        pytest.param(
            {"source_locator_kind": SourceLocatorKind.TEXT_PASSAGE},
            "TEXT_PASSAGE evidence must not define page_number",
            id="text-with-page",
        ),
        pytest.param(
            {
                "source_locator_kind": SourceLocatorKind.TEXT_PASSAGE,
                "page_number": None,
            },
            "TEXT_PASSAGE evidence requires source offsets",
            id="text-without-source-offsets",
        ),
        pytest.param(
            {
                "source_locator_kind": SourceLocatorKind.TEXT_PASSAGE,
                "page_number": None,
                "source_start_character": 10,
                "source_end_character": 11,
            },
            "text-passage offsets must match exact_text length",
            id="text-with-wrong-source-length",
        ),
    ],
)
def test_evidence_span_rejects_invalid_source_locator(
    changes: dict[str, object],
    expected_error: str,
) -> None:
    with pytest.raises(ValidationError, match=expected_error):
        EvidenceSpan.model_validate({**make_span().model_dump(), **changes})


def test_failed_validation_requires_safe_reason() -> None:
    with pytest.raises(ValidationError, match="requires reason"):
        make_validation(passed=False)

    failed = make_validation(passed=False, reason="  Source span did not match.  ")

    assert failed.reason == "Source span did not match."


def test_repository_matches_application_owned_protocol() -> None:
    session = create_autospec(Session, instance=True)

    repository: EvidenceRepository = SqlAlchemyEvidenceRepository(session)

    assert isinstance(repository, SqlAlchemyEvidenceRepository)


def test_repository_stages_span_only_in_matching_scope() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyEvidenceRepository(session)
    span = make_span()

    repository.add_span(make_context(), span)

    session.add.assert_called_once()
    row = session.add.call_args.args[0]
    assert isinstance(row, EvidenceSpanRow)
    assert row.to_domain() == span

    with pytest.raises(EvidenceScopeError, match="outside the active workspace"):
        repository.add_span(make_context(), make_span(workspace_id=OTHER_WORKSPACE_ID))


def test_repository_reads_span_without_cross_workspace_disclosure() -> None:
    session = create_autospec(Session, instance=True)
    session.scalar.return_value = EvidenceSpanRow.from_domain(make_span())
    repository = SqlAlchemyEvidenceRepository(session)

    assert repository.get_span_by_id(make_context(), EVIDENCE_SPAN_ID) == make_span()

    session.scalar.return_value = None
    assert repository.get_span_by_id(make_context(), OTHER_SPAN_ID) is None


def test_repository_reuses_identical_canonical_span() -> None:
    session = create_autospec(Session, instance=True)
    span = make_span()
    session.scalar.side_effect = [None, EvidenceSpanRow.from_domain(span)]
    repository = SqlAlchemyEvidenceRepository(session)

    assert repository.get_or_add_span(make_context(), span) == span
    assert session.scalar.call_count == 2


def test_repository_rejects_conflicting_text_passage_locator() -> None:
    session = create_autospec(Session, instance=True)
    source_start = 20
    span = EvidenceSpan.model_validate(
        {
            **make_span().model_dump(),
            "source_locator_kind": SourceLocatorKind.TEXT_PASSAGE,
            "page_number": None,
            "source_start_character": source_start,
            "source_end_character": source_start + len(EXACT_TEXT),
        }
    )
    conflicting_span = span.model_copy(
        update={
            "source_start_character": source_start + 1,
            "source_end_character": source_start + 1 + len(EXACT_TEXT),
        }
    )
    session.scalar.side_effect = [None, EvidenceSpanRow.from_domain(conflicting_span)]
    repository = SqlAlchemyEvidenceRepository(session)

    with pytest.raises(EvidenceRelationshipError, match="conflicts with its source content"):
        repository.get_or_add_span(make_context(), span)


def test_repository_stages_supporting_and_conflicting_evidence_in_order() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyEvidenceRepository(session)
    relationships = [
        make_relationship(),
        make_relationship(
            relationship_id=OTHER_RELATIONSHIP_ID,
            evidence_span_id=OTHER_SPAN_ID,
            relationship=EvidenceRelationship.CONFLICTS,
            display_order=1,
        ),
    ]

    repository.add_statement_evidence(make_context(), ANSWER_STATEMENT_ID, relationships)

    rows = list(session.add_all.call_args.args[0])
    assert [row.to_domain() for row in rows] == relationships


@pytest.mark.parametrize(
    ("relationships", "error_type", "expected_error"),
    [
        pytest.param(
            [],
            EvidenceRelationshipError,
            "requires evidence",
            id="empty",
        ),
        pytest.param(
            [make_relationship(workspace_id=OTHER_WORKSPACE_ID)],
            EvidenceScopeError,
            "outside the active workspace",
            id="outside-workspace",
        ),
        pytest.param(
            [make_relationship(answer_statement_id=OTHER_STATEMENT_ID)],
            EvidenceRelationshipError,
            "does not belong",
            id="wrong-statement",
        ),
        pytest.param(
            [
                make_relationship(),
                make_relationship(relationship_id=OTHER_RELATIONSHIP_ID, display_order=1),
            ],
            EvidenceRelationshipError,
            "duplicated",
            id="duplicate",
        ),
        pytest.param(
            [make_relationship(display_order=1)],
            EvidenceRelationshipError,
            "contiguous",
            id="order-gap",
        ),
        pytest.param(
            [make_relationship(relationship=EvidenceRelationship.CONFLICTS)],
            EvidenceRelationshipError,
            "supporting evidence",
            id="conflict-only",
        ),
    ],
)
def test_repository_rejects_inconsistent_statement_evidence(
    relationships: list[StatementEvidence],
    error_type: type[ValueError],
    expected_error: str,
) -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyEvidenceRepository(session)

    with pytest.raises(error_type, match=expected_error):
        repository.add_statement_evidence(
            make_context(),
            ANSWER_STATEMENT_ID,
            relationships,
        )

    session.add_all.assert_not_called()


def test_repository_lists_statement_evidence_in_persisted_order() -> None:
    session = create_autospec(Session, instance=True)
    relationship = make_relationship()
    session.scalars.return_value = (StatementEvidenceRow.from_domain(relationship),)
    repository = SqlAlchemyEvidenceRepository(session)

    assert repository.list_statement_evidence(make_context(), ANSWER_STATEMENT_ID) == (
        relationship,
    )


def test_repository_stages_answer_and_statement_validation_results() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyEvidenceRepository(session)
    validations = [
        make_validation(),
        make_validation(
            validation_id=OTHER_VALIDATION_ID,
            answer_statement_id=ANSWER_STATEMENT_ID,
            rule=ValidationRule.STATEMENT_SUPPORT,
        ),
    ]

    repository.add_answer_validations(make_context(), ANSWER_ID, validations)

    rows = list(session.add_all.call_args.args[0])
    assert [row.to_domain() for row in rows] == validations


@pytest.mark.parametrize(
    ("validations", "error_type", "expected_error"),
    [
        pytest.param(
            [],
            EvidenceRelationshipError,
            "requires validation results",
            id="empty",
        ),
        pytest.param(
            [make_validation(workspace_id=OTHER_WORKSPACE_ID)],
            EvidenceScopeError,
            "outside the active workspace",
            id="outside-workspace",
        ),
        pytest.param(
            [make_validation(answer_id=OTHER_SPAN_ID)],
            EvidenceRelationshipError,
            "does not belong",
            id="wrong-answer",
        ),
        pytest.param(
            [make_validation(), make_validation(validation_id=OTHER_VALIDATION_ID)],
            EvidenceRelationshipError,
            "duplicated",
            id="duplicate-rule",
        ),
        pytest.param(
            [
                make_validation(),
                make_validation(
                    validation_id=OTHER_VALIDATION_ID,
                    rule=ValidationRule.WORKSPACE_SCOPE,
                    config_version="evidence-validation-v2",
                ),
            ],
            EvidenceRelationshipError,
            "one configuration version",
            id="mixed-configurations",
        ),
    ],
)
def test_repository_rejects_inconsistent_validation_sets(
    validations: list[AnswerValidation],
    error_type: type[ValueError],
    expected_error: str,
) -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyEvidenceRepository(session)

    with pytest.raises(error_type, match=expected_error):
        repository.add_answer_validations(make_context(), ANSWER_ID, validations)

    session.add_all.assert_not_called()


def test_repository_lists_answer_validations_in_stable_order() -> None:
    session = create_autospec(Session, instance=True)
    validation = make_validation()
    session.scalars.return_value = (AnswerValidationRow.from_domain(validation),)
    repository = SqlAlchemyEvidenceRepository(session)

    assert repository.list_answer_validations(make_context(), ANSWER_ID) == (validation,)


def test_evidence_tables_enforce_scope_and_source_relationships() -> None:
    span_table = cast(Table, EvidenceSpanRow.__table__)
    relationship_table = cast(Table, StatementEvidenceRow.__table__)
    validation_table = cast(Table, AnswerValidationRow.__table__)

    for table in (span_table, relationship_table, validation_table):
        assert "workspace_id" in table.c
        target_sets = {
            tuple(element.target_fullname for element in constraint.elements)
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }
        assert ("workspaces.id",) in target_sets

    assert span_table.c.source_locator_kind.nullable is False
    assert span_table.c.page_number.nullable is True
    assert span_table.c.source_start_character.nullable is True
    assert span_table.c.source_end_character.nullable is True
    span_constraint_names = {constraint.name for constraint in span_table.constraints}
    assert "ck_evidence_spans_valid_source_locator" in span_constraint_names

    span_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in span_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert ("document_versions.workspace_id", "document_versions.id") in span_targets
    assert (
        "structure_blocks.workspace_id",
        "structure_blocks.document_version_id",
        "structure_blocks.id",
    ) in span_targets

    relationship_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in relationship_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert ("answer_statements.workspace_id", "answer_statements.id") in relationship_targets
    assert ("evidence_spans.workspace_id", "evidence_spans.id") in relationship_targets

    validation_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in validation_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert ("answers.workspace_id", "answers.id") in validation_targets
    assert (
        "answer_statements.workspace_id",
        "answer_statements.answer_id",
        "answer_statements.id",
    ) in validation_targets

    answer_rule_index = next(
        index
        for index in validation_table.indexes
        if index.name == "ux_answer_validations_answer_rule"
    )
    statement_rule_index = next(
        index
        for index in validation_table.indexes
        if index.name == "ux_answer_validations_statement_rule"
    )
    assert answer_rule_index.unique is True
    assert answer_rule_index.dialect_options["postgresql"]["where"] is not None
    assert statement_rule_index.unique is True
    assert statement_rule_index.dialect_options["postgresql"]["where"] is not None
