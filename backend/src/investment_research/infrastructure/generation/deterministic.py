"""Deterministic answer generator for the bounded synthetic M0.0 fixture."""

from dataclasses import dataclass
from unicodedata import normalize

from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
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


class DeterministicFixtureAnswerGenerator:
    """Generate only the configured answer when its exact evidence was retrieved."""

    def __init__(
        self,
        *,
        answerable_case: FixtureAnswerCase,
        insufficient_message: str,
    ) -> None:
        if not insufficient_message.strip():
            raise ValueError("insufficient_message must contain a non-whitespace character")
        self._answerable_case = answerable_case
        self._insufficient_message = insufficient_message

    def generate(self, generation_input: AnswerGenerationInput) -> UnvalidatedGeneratedAnswer:
        """Return a reproducible proposal without treating it as validated evidence."""

        if self._normalize_question(generation_input.question) != self._normalize_question(
            self._answerable_case.question
        ):
            return self._insufficient_result()

        for source in generation_input.sources:
            start_character = source.text.find(self._answerable_case.evidence_text)
            if start_character < 0:
                continue
            end_character = start_character + len(self._answerable_case.evidence_text)
            return UnvalidatedGeneratedAnswer(
                coverage_status=CoverageStatus.COMPLETE,
                rendered_text=self._answerable_case.statement,
                statements=(
                    UnvalidatedGeneratedStatement(
                        text=self._answerable_case.statement,
                        evidence=(
                            UnvalidatedEvidenceReference(
                                retrieval_candidate_id=source.retrieval_candidate_id,
                                start_character=start_character,
                                end_character=end_character,
                                exact_text=self._answerable_case.evidence_text,
                            ),
                        ),
                    ),
                ),
            )

        return self._insufficient_result()

    def _insufficient_result(self) -> UnvalidatedGeneratedAnswer:
        """Return the single safe result used when the configured evidence is unavailable."""

        return UnvalidatedGeneratedAnswer(
            coverage_status=CoverageStatus.INSUFFICIENT,
            rendered_text=self._insufficient_message,
            statements=(),
            missing_information=self._insufficient_message,
        )

    @staticmethod
    def _normalize_question(question: str) -> str:
        """Normalize harmless spelling variation for deterministic fixture matching."""

        return " ".join(normalize("NFKC", question).casefold().split())
