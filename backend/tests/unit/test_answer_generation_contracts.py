"""Unit tests for bounded answer-generation contracts."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.research.contracts import (
    M1_GENERATED_STATEMENT_LIMIT,
    M1_STATEMENT_EVIDENCE_LIMIT,
    AnswerGenerationInput,
    AnswerGenerationSource,
    UnvalidatedEvidenceReference,
    UnvalidatedGeneratedAnswer,
    UnvalidatedGeneratedStatement,
)
from investment_research.modules.research.domain import CoverageStatus
from investment_research.modules.search.contracts import (
    M1_CONTEXT_CHARACTER_LIMIT,
    M1_CONTEXT_CHUNK_LIMIT,
)


def make_source(ordinal: int, *, text: str = "Evidence.") -> AnswerGenerationSource:
    """Build one uniquely identified text source for boundary tests."""

    return AnswerGenerationSource(
        retrieval_candidate_id=UUID(int=ordinal * 10 + 1),
        chunk_id=UUID(int=ordinal * 10 + 2),
        document_id=UUID(int=ordinal * 10 + 3),
        document_version_id=UUID(int=ordinal * 10 + 4),
        structure_block_id=UUID(int=ordinal * 10 + 5),
        source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
        text=text,
    )


def make_reference(ordinal: int) -> UnvalidatedEvidenceReference:
    """Build one unique exact single-character evidence reference."""

    return UnvalidatedEvidenceReference(
        retrieval_candidate_id=UUID(int=ordinal + 1),
        start_character=ordinal,
        end_character=ordinal + 1,
        exact_text="E",
    )


def make_statement(ordinal: int, *, evidence_count: int = 1) -> UnvalidatedGeneratedStatement:
    """Build one statement with the requested number of unique references."""

    return UnvalidatedGeneratedStatement(
        text=f"Supported statement {ordinal}.",
        evidence=tuple(make_reference(index) for index in range(evidence_count)),
    )


def test_generation_contracts_accept_the_exact_m1_limits() -> None:
    source_text = "E" * (M1_CONTEXT_CHARACTER_LIMIT // M1_CONTEXT_CHUNK_LIMIT)
    generation_input = AnswerGenerationInput(
        research_request_id=UUID(int=1),
        question="What does the archive show?",
        sources=tuple(
            make_source(index + 1, text=source_text) for index in range(M1_CONTEXT_CHUNK_LIMIT)
        ),
    )
    answer = UnvalidatedGeneratedAnswer(
        coverage_status=CoverageStatus.COMPLETE,
        rendered_text="Bounded answer.",
        statements=tuple(
            make_statement(index, evidence_count=M1_STATEMENT_EVIDENCE_LIMIT)
            for index in range(M1_GENERATED_STATEMENT_LIMIT)
        ),
    )

    assert len(generation_input.sources) == M1_CONTEXT_CHUNK_LIMIT
    assert len(answer.statements) == M1_GENERATED_STATEMENT_LIMIT


def test_generation_input_rejects_too_many_sources() -> None:
    with pytest.raises(ValidationError, match="at most 32 items"):
        AnswerGenerationInput(
            research_request_id=UUID(int=1),
            question="What does the archive show?",
            sources=tuple(make_source(index + 1) for index in range(33)),
        )


def test_generation_input_rejects_excessive_combined_source_text() -> None:
    with pytest.raises(ValidationError, match="context character limit"):
        AnswerGenerationInput(
            research_request_id=UUID(int=1),
            question="What does the archive show?",
            sources=(make_source(1, text="E" * (M1_CONTEXT_CHARACTER_LIMIT + 1)),),
        )


def test_generated_answer_rejects_too_many_statements() -> None:
    with pytest.raises(ValidationError, match="at most 8 items"):
        UnvalidatedGeneratedAnswer(
            coverage_status=CoverageStatus.COMPLETE,
            rendered_text="Oversized answer.",
            statements=tuple(
                make_statement(index) for index in range(M1_GENERATED_STATEMENT_LIMIT + 1)
            ),
        )


def test_generated_statement_rejects_too_many_evidence_references() -> None:
    with pytest.raises(ValidationError, match="at most 2 items"):
        make_statement(1, evidence_count=M1_STATEMENT_EVIDENCE_LIMIT + 1)
