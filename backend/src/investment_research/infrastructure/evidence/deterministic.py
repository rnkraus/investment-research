"""Deterministic statement-support evaluation for the synthetic M0.0 fixture."""

from collections.abc import Sequence
from dataclasses import dataclass


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

    def supports(self, statement: str, evidence_texts: Sequence[str]) -> bool:
        """Return whether one configured pair occurs in the canonical evidence set."""

        evidence_set = set(evidence_texts)
        return any(
            statement == rule.statement and rule.evidence_text in evidence_set
            for rule in self._rules
        )
