"""Unit tests for lexical-search query construction and contracts."""

from datetime import UTC, datetime, timedelta, timezone
from math import inf
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.search.application import (
    build_lexical_search_query,
    normalize_lexical_query,
)
from investment_research.modules.search.contracts import LexicalSearchHit
from investment_research.modules.search.domain import RetrievalFilters

DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000002")
BLOCK_ID = UUID("10000000-0000-4000-8000-000000000003")
CHUNK_ID = UUID("10000000-0000-4000-8000-000000000004")
CUTOFF_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)


def make_hit(**changes: object) -> LexicalSearchHit:
    """Build one valid lexical hit with optional field changes."""

    values = {
        "chunk_id": CHUNK_ID,
        "document_id": DOCUMENT_ID,
        "document_version_id": VERSION_ID,
        "structure_block_id": BLOCK_ID,
        "page_number": 2,
        "text": "FY2025 revenue was $184.2 million.",
        "raw_score": 0.75,
        **changes,
    }
    return LexicalSearchHit.model_validate(values)


def test_normalize_lexical_query_canonicalizes_unicode_case_and_whitespace() -> None:
    query = "  \uff26\uff39\uff12\uff10\uff12\uff15\tREVENUE\nGrowth  "

    assert normalize_lexical_query(query) == "fy2025 revenue growth"


@pytest.mark.parametrize(
    "query",
    [" \n\t ", "x" * 2_001],
    ids=["no-visible-text", "over-character-limit"],
)
def test_normalize_lexical_query_rejects_invalid_input(query: str) -> None:
    with pytest.raises(ValidationError):
        normalize_lexical_query(query)


def test_build_lexical_search_query_preserves_effective_filters() -> None:
    filters = RetrievalFilters(
        document_ids=(DOCUMENT_ID,),
        available_at_cutoff=CUTOFF_AT,
        captured_at_cutoff=CUTOFF_AT,
    )

    query = build_lexical_search_query(
        query_text="  What was FY2025 REVENUE?  ",
        filters=filters,
        lexical_config_version="m0.0-english-lexical-v1",
        candidate_limit=10,
    )

    assert query.normalized_query == "what was fy2025 revenue?"
    assert query.filters is filters
    assert query.lexical_config_version == "m0.0-english-lexical-v1"
    assert query.candidate_limit == 10


@pytest.mark.parametrize("candidate_limit", [0, 101])
def test_build_lexical_search_query_rejects_unbounded_candidate_limits(
    candidate_limit: int,
) -> None:
    with pytest.raises(ValidationError):
        build_lexical_search_query(
            query_text="What was FY2025 revenue?",
            filters=RetrievalFilters(),
            lexical_config_version="m0.0-english-lexical-v1",
            candidate_limit=candidate_limit,
        )


def test_archive_cutoff_requires_public_availability_cutoff() -> None:
    with pytest.raises(ValidationError, match="requires available_at_cutoff"):
        RetrievalFilters(captured_at_cutoff=CUTOFF_AT)


def test_temporal_filters_normalize_to_utc() -> None:
    offset_cutoff = CUTOFF_AT.astimezone(timezone(timedelta(hours=2)))

    filters = RetrievalFilters(
        available_at_cutoff=offset_cutoff,
        captured_at_cutoff=offset_cutoff,
    )

    assert filters.available_at_cutoff == CUTOFF_AT
    assert filters.captured_at_cutoff == CUTOFF_AT


@pytest.mark.parametrize(
    "changes",
    [
        {"text": " \n\t "},
        {"page_number": 0},
        {"raw_score": 0},
        {"raw_score": inf},
    ],
    ids=["no-visible-text", "invalid-page", "zero-score", "infinite-score"],
)
def test_lexical_search_hit_rejects_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_hit(**changes)
