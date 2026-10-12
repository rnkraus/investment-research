"""Load non-secret research tuning with the standard-library TOML parser."""

from pathlib import Path
from tomllib import TOMLDecodeError, loads

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from investment_research.modules.search.contracts import (
    M1_CONTEXT_CHARACTER_LIMIT,
    M1_CONTEXT_CHUNK_LIMIT,
    M1_LANE_CANDIDATE_LIMIT,
)
from investment_research.modules.search.limits import MAX_CONTEXT_CHUNKS


class StrictRagSettings(BaseModel):
    """Reject unknown keys, booleans, strings, and coercion of numeric settings."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)


class RetrievalTuning(StrictRagSettings):
    """Bound each lane independently before deduplication and context selection."""

    lexical_candidate_limit: int = Field(default=M1_LANE_CANDIDATE_LIMIT, ge=1, le=20)
    dense_candidate_limit: int = Field(default=M1_LANE_CANDIDATE_LIMIT, ge=1, le=20)


class ContextTuning(StrictRagSettings):
    """Limit whole chunks without increasing the existing text or provider budgets."""

    chunk_limit: int = Field(default=M1_CONTEXT_CHUNK_LIMIT, ge=1, le=MAX_CONTEXT_CHUNKS)
    character_limit: int = Field(default=M1_CONTEXT_CHARACTER_LIMIT, ge=1, le=24_000)


class RagSettings(StrictRagSettings):
    """Immutable startup snapshot; programmatic tests retain frozen defaults."""

    retrieval: RetrievalTuning = Field(default_factory=RetrievalTuning)
    context: ContextTuning = Field(default_factory=ContextTuning)


class RagConfigurationError(ValueError):
    """A safe startup failure without arbitrary file contents or values."""


def load_rag_settings(path: Path) -> RagSettings:
    """Require a complete, valid file instead of silently falling back on a typo."""

    try:
        values = loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, TOMLDecodeError:
        raise RagConfigurationError(
            "config/rag.toml is missing, unreadable, or invalid TOML"
        ) from None
    required = {
        "retrieval": {"lexical_candidate_limit", "dense_candidate_limit"},
        "context": {"chunk_limit", "character_limit"},
    }
    for section, keys in required.items():
        content = values.get(section)
        if not isinstance(content, dict) or not keys.issubset(content):
            raise RagConfigurationError(f"config/rag.toml requires all keys in [{section}]")
    try:
        return RagSettings.model_validate(values)
    except ValidationError as error:
        fields = ", ".join(".".join(map(str, item["loc"])) for item in error.errors())
        raise RagConfigurationError(f"config/rag.toml has invalid settings: {fields}") from None
