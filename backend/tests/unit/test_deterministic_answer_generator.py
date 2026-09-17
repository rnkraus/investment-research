"""Unit tests for the bounded deterministic M0.0 answer generator."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.infrastructure.generation.deterministic import (
    M0_0_FIXTURE_ANSWER_CASE,
    M0_0_INSUFFICIENT_MESSAGE,
    DeterministicFixtureAnswerGenerator,
    FixtureAnswerCase,
)
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationSource,
    UnvalidatedGeneratedAnswer,
)
from investment_research.modules.research.domain import CoverageStatus
from investment_research.modules.research.ports import AnswerGenerator

REQUEST_ID = UUID("10000000-0000-4000-8000-000000000001")
CANDIDATE_ID = UUID("20000000-0000-4000-8000-000000000001")
CHUNK_ID = UUID("30000000-0000-4000-8000-000000000001")
DOCUMENT_ID = UUID("40000000-0000-4000-8000-000000000001")
VERSION_ID = UUID("50000000-0000-4000-8000-000000000001")
BLOCK_ID = UUID("60000000-0000-4000-8000-000000000001")
QUESTION = M0_0_FIXTURE_ANSWER_CASE.question
EVIDENCE_TEXT = M0_0_FIXTURE_ANSWER_CASE.evidence_text
STATEMENT = M0_0_FIXTURE_ANSWER_CASE.statement
INSUFFICIENT_MESSAGE = M0_0_INSUFFICIENT_MESSAGE


def make_generator() -> AnswerGenerator:
    """Return the adapter through the application-owned port type."""

    return DeterministicFixtureAnswerGenerator(
        answerable_case=M0_0_FIXTURE_ANSWER_CASE,
        insufficient_message=INSUFFICIENT_MESSAGE,
    )


def make_source(text: str) -> AnswerGenerationSource:
    """Build one selected retrieval source around caller-controlled text."""

    return AnswerGenerationSource(
        retrieval_candidate_id=CANDIDATE_ID,
        chunk_id=CHUNK_ID,
        document_id=DOCUMENT_ID,
        document_version_id=VERSION_ID,
        structure_block_id=BLOCK_ID,
        source_locator_kind=SourceLocatorKind.PDF_PAGE,
        page_number=2,
        text=text,
    )


def make_input(
    *, question: str = QUESTION, source_text: str | None = None
) -> AnswerGenerationInput:
    """Build a bounded generation input with zero or one retrieval source."""

    sources = () if source_text is None else (make_source(source_text),)
    return AnswerGenerationInput(
        research_request_id=REQUEST_ID,
        question=question,
        sources=sources,
    )


def test_answerable_fixture_returns_the_same_exactly_anchored_proposal() -> None:
    source_text = f"Summary. {EVIDENCE_TEXT} Outlook remained unchanged."
    generation_input = make_input(
        question=f"  {QUESTION.upper()}  ",
        source_text=source_text,
    )
    generator = make_generator()

    first_result = generator.generate(generation_input)
    second_result = generator.generate(generation_input)

    assert first_result == second_result
    assert first_result.coverage_status is CoverageStatus.COMPLETE
    assert first_result.rendered_text == STATEMENT
    assert len(first_result.statements) == 1
    statement = first_result.statements[0]
    assert statement.text == STATEMENT
    assert len(statement.evidence) == 1
    reference = statement.evidence[0]
    assert reference.retrieval_candidate_id == CANDIDATE_ID
    assert source_text[reference.start_character : reference.end_character] == EVIDENCE_TEXT
    assert reference.exact_text == EVIDENCE_TEXT


@pytest.mark.parametrize(
    "generation_input",
    [
        make_input(
            question="What dividend did Northstar Grid Analytics declare for FY2025?",
            source_text=EVIDENCE_TEXT,
        ),
        make_input(question=QUESTION),
        make_input(question=QUESTION, source_text="FY2025 revenue information is unavailable."),
    ],
)
def test_unanswerable_or_unsupported_input_returns_explicit_insufficient_coverage(
    generation_input: AnswerGenerationInput,
) -> None:
    result = make_generator().generate(generation_input)

    assert result.coverage_status is CoverageStatus.INSUFFICIENT
    assert result.rendered_text == INSUFFICIENT_MESSAGE
    assert result.statements == ()
    assert result.missing_information == INSUFFICIENT_MESSAGE


def test_generation_contract_rejects_statements_claimed_as_insufficient() -> None:
    answered = make_generator().generate(make_input(question=QUESTION, source_text=EVIDENCE_TEXT))

    with pytest.raises(
        ValidationError, match="insufficient generation must not contain statements"
    ):
        UnvalidatedGeneratedAnswer.model_validate(
            {
                **answered.model_dump(),
                "coverage_status": CoverageStatus.INSUFFICIENT,
                "missing_information": INSUFFICIENT_MESSAGE,
            }
        )


@pytest.mark.parametrize("field_name", ["question", "statement", "evidence_text"])
def test_fixture_configuration_rejects_blank_required_values(field_name: str) -> None:
    values = {
        "question": QUESTION,
        "statement": STATEMENT,
        "evidence_text": EVIDENCE_TEXT,
    }
    values[field_name] = "   "

    with pytest.raises(ValueError, match=field_name):
        FixtureAnswerCase(**values)
