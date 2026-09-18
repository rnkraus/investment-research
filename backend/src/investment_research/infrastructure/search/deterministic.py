"""Deterministic embedding adapter for credential-free correctness tests."""

from dataclasses import dataclass
from math import isfinite

from pydantic import TypeAdapter

from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    EmbeddingPurpose,
    EmbeddingRequest,
    EmbeddingVector,
)
from investment_research.modules.search.domain import ConfigurationVersion
from investment_research.modules.search.ports import SearchProviderConfigurationError

_CONFIGURATION_VERSION_ADAPTER = TypeAdapter(ConfigurationVersion)


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

    def embed(self, request: EmbeddingRequest) -> tuple[EmbeddingVector, ...]:
        """Return one fixed vector per input while preserving correlation and order."""

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
        return tuple(vectors)
