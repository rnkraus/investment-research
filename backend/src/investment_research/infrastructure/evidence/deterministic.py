"""Deterministic statement-support evaluation for the synthetic M0.0 fixture."""

from collections.abc import Sequence
from dataclasses import dataclass

from investment_research.modules.evidence.contracts import (
    StatementSupportDecision,
    StatementSupportResult,
)
from investment_research.modules.operations.domain import ProviderUsage


@dataclass(frozen=True, slots=True)
class FixtureStatementSupportRule:
    """One exact statement and canonical evidence pair accepted by the fixture proof."""

    statement: str
    evidence_text: str

    def __post_init__(self) -> None:
        """Reject unusable support rules at application startup."""

        if not self.statement.strip():
            raise ValueError("statement must contain a non-whitespace character")
        if not self.evidence_text.strip():
            raise ValueError("evidence_text must contain a non-whitespace character")


class DeterministicFixtureStatementSupportEvaluator:
    """Accept only explicitly configured statement and exact evidence pairs."""

    def __init__(self, rules: Sequence[FixtureStatementSupportRule]) -> None:
        if not rules:
            raise ValueError("at least one fixture statement support rule is required")
        self._rules = tuple(rules)

    def evaluate(self, statement: str, evidence_texts: Sequence[str]) -> StatementSupportResult:
        """Return the fixture decision with zero external-provider usage."""

        evidence_set = set(evidence_texts)
        return StatementSupportResult(
            decision=StatementSupportDecision(
                supported=any(
                    statement == rule.statement and rule.evidence_text in evidence_set
                    for rule in self._rules
                )
            ),
            usage=ProviderUsage(input_units=0, output_units=0),
        )
