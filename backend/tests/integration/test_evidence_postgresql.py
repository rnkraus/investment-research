"""PostgreSQL integration tests for scoped evidence persistence."""

from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.modules.evidence.domain import (
    AnswerValidation,
    EvidenceRelationship,
    EvidenceSpan,
    StatementEvidence,
    ValidationRule,
)
from investment_research.modules.evidence.persistence import (
    AnswerValidationRow,
    SqlAlchemyEvidenceRepository,
)
from tests.integration.factories import (
    FIXTURE_TIME,
    AnswerResultFixture,
    ResearchSource,
    make_answer_result,
    make_research_source,
    make_retrieval_result,
    persist_answer_result,
    persist_research_source,
    persist_retrieval_result,
    persist_workspace,
)


def make_span(
    source: ResearchSource,
    *,
    block_index: int,
    workspace_id: UUID | None = None,
    document_version_id: UUID | None = None,
    structure_block_id: UUID | None = None,
) -> EvidenceSpan:
    """Build an exact full-block span with a matching UTF-8 hash."""

    block = source.blocks[block_index]
    return EvidenceSpan(
        id=uuid4(),
        workspace_id=workspace_id or source.context.workspace_id,
        document_version_id=document_version_id or source.version.id,
        structure_block_id=structure_block_id or block.id,
        page_number=block.page_number,
        start_character=0,
        end_character=len(block.text),
        exact_text=block.text,
        text_sha256=sha256(block.text.encode("utf-8")).hexdigest(),
        created_at=FIXTURE_TIME,
    )


def make_relationship(
    source: ResearchSource,
    answer_result: AnswerResultFixture,
    span: EvidenceSpan,
    *,
    relationship: EvidenceRelationship,
    display_order: int,
    answer_statement_id: UUID | None = None,
) -> StatementEvidence:
    """Build one ordered relationship between an answer statement and a span."""

    return StatementEvidence(
        id=uuid4(),
        workspace_id=source.context.workspace_id,
        answer_statement_id=answer_statement_id or answer_result.statements[0].id,
        evidence_span_id=span.id,
        relationship=relationship,
        display_order=display_order,
        created_at=FIXTURE_TIME,
    )


def make_validation(
    source: ResearchSource,
    answer_result: AnswerResultFixture,
    *,
    rule: ValidationRule,
    answer_statement_id: UUID | None = None,
) -> AnswerValidation:
    """Build one passing answer- or statement-level validation result."""

    return AnswerValidation(
        id=uuid4(),
        workspace_id=source.context.workspace_id,
        answer_id=answer_result.answer.id,
        answer_statement_id=answer_statement_id,
        rule=rule,
        passed=True,
        validation_config_version="evidence-validation-v1",
        validated_at=FIXTURE_TIME,
    )


def persist_answer_prerequisites(
    engine: Engine,
    source: ResearchSource,
) -> AnswerResultFixture:
    """Persist a source, retrieval result, and streamed answer for evidence tests."""

    persist_research_source(engine, source)
    retrieval = make_retrieval_result(source)
    persist_retrieval_result(engine, source, retrieval)
    answer_result = make_answer_result(source, retrieval)
    persist_answer_result(engine, source, answer_result)
    return answer_result


def test_complete_evidence_chain_round_trips_in_one_scoped_transaction(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Evidence Source")
    foreign_source = make_research_source(label="Foreign Evidence Source")
    answer_result = persist_answer_prerequisites(integration_database_engine, source)
    persist_workspace(integration_database_engine, foreign_source)
    supporting_span = make_span(source, block_index=0)
    conflicting_span = make_span(source, block_index=1)
    relationships = (
        make_relationship(
            source,
            answer_result,
            supporting_span,
            relationship=EvidenceRelationship.SUPPORTS,
            display_order=0,
        ),
        make_relationship(
            source,
            answer_result,
            conflicting_span,
            relationship=EvidenceRelationship.CONFLICTS,
            display_order=1,
        ),
    )
    statement_id = answer_result.statements[0].id
    validations = (
        make_validation(source, answer_result, rule=ValidationRule.COVERAGE),
        make_validation(source, answer_result, rule=ValidationRule.WORKSPACE_SCOPE),
        make_validation(
            source,
            answer_result,
            rule=ValidationRule.EXACT_SOURCE_SPAN,
            answer_statement_id=statement_id,
        ),
        make_validation(
            source,
            answer_result,
            rule=ValidationRule.STATEMENT_SUPPORT,
            answer_statement_id=statement_id,
        ),
    )

    with transactional_session(session_factory) as session:
        repository = SqlAlchemyEvidenceRepository(session)
        repository.add_span(source.context, supporting_span)
        repository.add_span(source.context, conflicting_span)
        repository.add_statement_evidence(source.context, statement_id, relationships)
        repository.add_answer_validations(
            source.context,
            answer_result.answer.id,
            validations,
        )

    with transactional_session(session_factory) as session:
        repository = SqlAlchemyEvidenceRepository(session)
        assert repository.get_span_by_id(source.context, supporting_span.id) == supporting_span
        assert repository.list_statement_evidence(source.context, statement_id) == relationships
        assert (
            repository.list_answer_validations(
                source.context,
                answer_result.answer.id,
            )
            == validations
        )
        assert repository.get_span_by_id(foreign_source.context, supporting_span.id) is None
        assert repository.list_statement_evidence(foreign_source.context, statement_id) == ()
        assert (
            repository.list_answer_validations(
                foreign_source.context,
                answer_result.answer.id,
            )
            == ()
        )


def test_database_rejects_span_linked_to_foreign_source(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Span Source")
    foreign_source = make_research_source(label="Foreign Span Source")
    persist_research_source(integration_database_engine, source)
    persist_workspace(integration_database_engine, foreign_source)
    cross_workspace_span = make_span(
        source,
        block_index=0,
        workspace_id=foreign_source.context.workspace_id,
    )

    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        SqlAlchemyEvidenceRepository(session).add_span(
            foreign_source.context,
            cross_workspace_span,
        )


def test_database_rejects_statement_relationship_to_foreign_span(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Relationship Source")
    foreign_source = make_research_source(label="Foreign Relationship Source")
    answer_result = persist_answer_prerequisites(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign_source)
    foreign_span = make_span(foreign_source, block_index=0)
    with transactional_session(session_factory) as session:
        SqlAlchemyEvidenceRepository(session).add_span(foreign_source.context, foreign_span)

    relationship = make_relationship(
        source,
        answer_result,
        foreign_span,
        relationship=EvidenceRelationship.SUPPORTS,
        display_order=0,
    )
    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        SqlAlchemyEvidenceRepository(session).add_statement_evidence(
            source.context,
            answer_result.statements[0].id,
            [relationship],
        )


def test_database_rejects_foreign_statement_validation_and_duplicate_rules(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Validation Source")
    foreign_source = make_research_source(label="Foreign Validation Source")
    answer_result = persist_answer_prerequisites(integration_database_engine, source)
    foreign_answer_result = persist_answer_prerequisites(
        integration_database_engine,
        foreign_source,
    )
    foreign_statement_validation = make_validation(
        source,
        answer_result,
        rule=ValidationRule.STATEMENT_SUPPORT,
        answer_statement_id=foreign_answer_result.statements[0].id,
    )
    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        SqlAlchemyEvidenceRepository(session).add_answer_validations(
            source.context,
            answer_result.answer.id,
            [foreign_statement_validation],
        )

    answer_validation = make_validation(
        source,
        answer_result,
        rule=ValidationRule.COVERAGE,
    )
    statement_validation = make_validation(
        source,
        answer_result,
        rule=ValidationRule.EXACT_SOURCE_SPAN,
        answer_statement_id=answer_result.statements[0].id,
    )
    with transactional_session(session_factory) as session:
        SqlAlchemyEvidenceRepository(session).add_answer_validations(
            source.context,
            answer_result.answer.id,
            [answer_validation, statement_validation],
        )

    for duplicate in (
        answer_validation.model_copy(update={"id": uuid4()}),
        statement_validation.model_copy(update={"id": uuid4()}),
    ):
        with (
            pytest.raises(IntegrityError),
            transactional_session(session_factory) as session,
        ):
            session.add(AnswerValidationRow.from_domain(duplicate))
