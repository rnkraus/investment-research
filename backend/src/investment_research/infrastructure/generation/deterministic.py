"""Deterministic answer generator for the bounded synthetic M0.0 fixture."""

from collections.abc import Sequence
from dataclasses import dataclass
from unicodedata import normalize

from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationResult,
    UnvalidatedEvidenceReference,
    UnvalidatedGeneratedAnswer,
    UnvalidatedGeneratedStatement,
)
from investment_research.modules.research.domain import CoverageStatus


@dataclass(frozen=True, slots=True)
class FixtureAnswerCase:
    """One configured fixture question, answer statement, and exact source passage."""

    question: str
    statement: str
    evidence_text: str

    def __post_init__(self) -> None:
        """Reject unusable fixture configuration at application startup."""

        for field_name in ("question", "statement", "evidence_text"):
            value = getattr(self, field_name)
            if not value.strip():
                raise ValueError(f"{field_name} must contain a non-whitespace character")


M0_0_FIXTURE_ANSWER_CASE = FixtureAnswerCase(
    question=(
        "What was Northstar Grid Analytics' FY2025 revenue, and how did it change from FY2024?"
    ),
    statement=(
        "Northstar Grid Analytics reported FY2025 revenue of $184.2 million, "
        "up 22.0% from $151.0 million in FY2024."
    ),
    evidence_text=("FY2025 revenue was $184.2 million, up 22.0% from $151.0 million in FY2024."),
)
M0_0_INSUFFICIENT_MESSAGE = (
    "The archive does not contain sufficient evidence to answer this question."
)


class DeterministicFixtureSetAnswerGenerator:
    """Generate only explicitly configured answers backed by exact retrieved evidence."""

    def __init__(
        self,
        *,
        answerable_cases: Sequence[FixtureAnswerCase],
        insufficient_message: str,
    ) -> None:
        if not answerable_cases:
            raise ValueError("at least one answerable fixture case is required")
        if not insufficient_message.strip():
            raise ValueError("insufficient_message must contain a non-whitespace character")
        cases_by_question: dict[str, FixtureAnswerCase] = {}
        for case in answerable_cases:
            normalized_question = self._normalize_question(case.question)
            if normalized_question in cases_by_question:
                raise ValueError("answerable fixture questions must be unique")
            cases_by_question[normalized_question] = case
        self._cases_by_question = cases_by_question
        self._insufficient_message = insufficient_message

    def generate(self, generation_input: AnswerGenerationInput) -> AnswerGenerationResult:
        """Return a reproducible proposal without treating it as validated evidence."""

        answerable_case = self._cases_by_question.get(
            self._normalize_question(generation_input.question)
        )
        if answerable_case is None:
            return self._insufficient_result()

        for source in generation_input.sources:
            start_character = source.text.find(answerable_case.evidence_text)
            if start_character < 0:
                continue
            end_character = start_character + len(answerable_case.evidence_text)
            return AnswerGenerationResult(
                answer=UnvalidatedGeneratedAnswer(
                    coverage_status=CoverageStatus.COMPLETE,
                    rendered_text=answerable_case.statement,
                    statements=(
                        UnvalidatedGeneratedStatement(
                            text=answerable_case.statement,
                            evidence=(
                                UnvalidatedEvidenceReference(
                                    retrieval_candidate_id=source.retrieval_candidate_id,
                                    start_character=start_character,
                                    end_character=end_character,
                                    exact_text=answerable_case.evidence_text,
                                ),
                            ),
                        ),
                    ),
                ),
                usage=ProviderUsage(input_units=0, output_units=0),
            )

        return self._insufficient_result()

    def _insufficient_result(self) -> AnswerGenerationResult:
        """Return the single safe result used when the configured evidence is unavailable."""

        return AnswerGenerationResult(
            answer=UnvalidatedGeneratedAnswer(
                coverage_status=CoverageStatus.INSUFFICIENT,
                rendered_text=self._insufficient_message,
                statements=(),
                missing_information=self._insufficient_message,
            ),
            usage=ProviderUsage(input_units=0, output_units=0),
        )

    @staticmethod
    def _normalize_question(question: str) -> str:
        """Normalize harmless spelling variation for deterministic fixture matching."""

        return " ".join(normalize("NFKC", question).casefold().split())


class DeterministicFixtureAnswerGenerator(DeterministicFixtureSetAnswerGenerator):
    """Retain the original one-case constructor used by the M0 regression path."""

    def __init__(
        self,
        *,
        answerable_case: FixtureAnswerCase,
        insufficient_message: str,
    ) -> None:
        super().__init__(
            answerable_cases=(answerable_case,),
            insufficient_message=insufficient_message,
        )
