"""Deterministic embedding adapter for credential-free correctness tests."""

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from math import isfinite

from pydantic import TypeAdapter

from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    EmbeddingPurpose,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingVector,
)
from investment_research.modules.search.domain import ConfigurationVersion
from investment_research.modules.search.ports import SearchProviderConfigurationError

_CONFIGURATION_VERSION_ADAPTER = TypeAdapter(ConfigurationVersion)


class DeterministicTextMatch(StrEnum):
    """Explicit text relationship allowed by one deterministic embedding rule."""

    EXACT = "EXACT"
    CONTAINS = "CONTAINS"


@dataclass(frozen=True, slots=True)
class DeterministicEmbeddingCase:
    """Map one exact purpose-and-text pair to one fixed finite vector."""

    purpose: EmbeddingPurpose
    text: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        """Reject cases that could not satisfy the production embedding contract."""

        if not self.text.strip():
            raise ValueError("deterministic embedding text must contain visible content")
        if len(self.values) != M1_EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"deterministic embedding must contain {M1_EMBEDDING_DIMENSIONS} values"
            )
        if any(not isfinite(value) for value in self.values):
            raise ValueError("deterministic embedding values must be finite")


class DeterministicEmbeddingProvider:
    """Return explicitly configured vectors without network access or hidden semantics."""

    def __init__(
        self,
        *,
        embedding_config_version: str,
        cases: tuple[DeterministicEmbeddingCase, ...],
    ) -> None:
        self._embedding_config_version = _CONFIGURATION_VERSION_ADAPTER.validate_python(
            embedding_config_version
        )
        if not cases:
            raise ValueError("at least one deterministic embedding case is required")

        vectors_by_input: dict[tuple[EmbeddingPurpose, str], tuple[float, ...]] = {}
        for case in cases:
            key = (case.purpose, case.text)
            if key in vectors_by_input:
                raise ValueError("deterministic embedding cases must be unique")
            vectors_by_input[key] = case.values
        self._vectors_by_input = vectors_by_input

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """Return fixed vectors and zero external usage in stable input order."""

        if request.embedding_config_version != self._embedding_config_version:
            raise SearchProviderConfigurationError(
                "deterministic embedding configuration version does not match the request"
            )

        vectors: list[EmbeddingVector] = []
        for item in request.inputs:
            values = self._vectors_by_input.get((item.purpose, item.text))
            if values is None:
                raise SearchProviderConfigurationError(
                    "deterministic embedding case is not configured"
                )
            vectors.append(
                EmbeddingVector(
                    input_id=item.input_id,
                    values=values,
                    embedding_config_version=self._embedding_config_version,
                )
            )
        return EmbeddingResult(
            vectors=tuple(vectors),
            usage=ProviderUsage(input_units=0, output_units=0),
        )


@dataclass(frozen=True, slots=True)
class DeterministicEmbeddingRule:
    """Map one explicit fixture text relationship to one reserved vector dimension."""

    purpose: EmbeddingPurpose
    text: str
    match: DeterministicTextMatch
    dimension: int

    def __post_init__(self) -> None:
        """Reject rules that could not produce a unique bounded fixture vector."""

        if not self.text.strip():
            raise ValueError("deterministic embedding rule text must contain visible content")
        if not 0 <= self.dimension < M1_EMBEDDING_DIMENSIONS:
            raise ValueError("deterministic embedding rule dimension is out of range")

    def matches(self, purpose: EmbeddingPurpose, text: str) -> bool:
        """Return whether this rule explicitly covers one provider input."""

        if purpose is not self.purpose:
            return False
        if self.match is DeterministicTextMatch.EXACT:
            return text == self.text
        return self.text in text


class DeterministicRuleEmbeddingProvider:
    """Create stable fixture vectors with explicit semantic aliases and no network use."""

    def __init__(
        self,
        *,
        embedding_config_version: str,
        rules: tuple[DeterministicEmbeddingRule, ...],
    ) -> None:
        self._embedding_config_version = _CONFIGURATION_VERSION_ADAPTER.validate_python(
            embedding_config_version
        )
        rule_keys = [(rule.purpose, rule.match, rule.text) for rule in rules]
        if len(rule_keys) != len(set(rule_keys)):
            raise ValueError("deterministic embedding rules must be unique")
        self._rules = rules
        reserved_dimensions = {rule.dimension for rule in rules}
        self._fallback_dimensions = tuple(
            dimension
            for dimension in range(M1_EMBEDDING_DIMENSIONS)
            if dimension not in reserved_dimensions
        )
        if not self._fallback_dimensions:
            raise ValueError("deterministic embedding rules must leave one fallback dimension")

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """Return stable nonzero vectors while honoring only configured semantic aliases."""

        if request.embedding_config_version != self._embedding_config_version:
            raise SearchProviderConfigurationError(
                "deterministic embedding configuration version does not match the request"
            )

        vectors: list[EmbeddingVector] = []
        for item in request.inputs:
            matching_rules = tuple(
                rule for rule in self._rules if rule.matches(item.purpose, item.text)
            )
            if len(matching_rules) > 1:
                raise SearchProviderConfigurationError(
                    "deterministic embedding rules matched one input ambiguously"
                )
            dimension = (
                matching_rules[0].dimension
                if matching_rules
                else self._fallback_dimension(item.purpose, item.text)
            )
            values = tuple(
                1.0 if position == dimension else 0.0 for position in range(M1_EMBEDDING_DIMENSIONS)
            )
            vectors.append(
                EmbeddingVector(
                    input_id=item.input_id,
                    values=values,
                    embedding_config_version=self._embedding_config_version,
                )
            )
        return EmbeddingResult(
            vectors=tuple(vectors),
            usage=ProviderUsage(input_units=0, output_units=0),
        )

    def _fallback_dimension(self, purpose: EmbeddingPurpose, text: str) -> int:
        """Keep unrelated fixture inputs stable and outside explicitly reserved dimensions."""

        digest = sha256(f"{purpose.value}\0{text}".encode()).digest()
        offset = int.from_bytes(digest[:8], byteorder="big") % len(self._fallback_dimensions)
        return self._fallback_dimensions[offset]
