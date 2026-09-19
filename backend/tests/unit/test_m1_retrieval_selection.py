"""Unit tests for deterministic bounded M1 context selection."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.search.contracts import (
    M1_SELECTOR_CONFIG_VERSION,
    LexicalSearchHit,
    M1SelectionProfile,
    QualifiedDenseHit,
)
from investment_research.modules.search.domain import RetrievalOrigin
from investment_research.modules.search.selection import select_m1_context

DOCUMENT_ID = UUID(int=101)
DOCUMENT_VERSION_ID = UUID(int=102)
STRUCTURE_BLOCK_ID = UUID(int=103)
FIRST_CHUNK_ID = UUID(int=201)
SECOND_CHUNK_ID = UUID(int=202)
THIRD_CHUNK_ID = UUID(int=203)
FOURTH_CHUNK_ID = UUID(int=204)


def make_lexical_hit(
    chunk_id: UUID,
    *,
    text: str,
    raw_score: float = 0.75,
) -> LexicalSearchHit:
    """Build one canonical lexical lane hit."""

    return LexicalSearchHit(
        chunk_id=chunk_id,
        document_id=DOCUMENT_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        structure_block_id=STRUCTURE_BLOCK_ID,
        source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
        text=text,
        raw_score=raw_score,
    )


def make_dense_hit(
    chunk_id: UUID,
    *,
    text: str,
    raw_score: float = -0.125,
) -> QualifiedDenseHit:
    """Build one canonically qualified dense lane hit."""

    return QualifiedDenseHit(
        chunk_id=chunk_id,
        document_id=DOCUMENT_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        structure_block_id=STRUCTURE_BLOCK_ID,
        source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
        text=text,
        raw_score=raw_score,
    )


def test_selector_interleaves_lexical_first_without_comparing_scores() -> None:
    lexical_hits = (
        make_lexical_hit(FIRST_CHUNK_ID, text="lexical one", raw_score=0.1),
        make_lexical_hit(THIRD_CHUNK_ID, text="lexical two", raw_score=0.9),
    )
    dense_hits = (
        make_dense_hit(SECOND_CHUNK_ID, text="dense one", raw_score=-0.9),
        make_dense_hit(FOURTH_CHUNK_ID, text="dense two", raw_score=0.99),
    )

    result = select_m1_context(
        lexical_hits=lexical_hits,
        dense_hits=dense_hits,
        profile=M1SelectionProfile(),
    )

    assert [candidate.chunk_id for candidate in result.candidates] == [
        FIRST_CHUNK_ID,
        SECOND_CHUNK_ID,
        THIRD_CHUNK_ID,
        FOURTH_CHUNK_ID,
    ]
    assert [candidate.origin for candidate in result.candidates] == [
        RetrievalOrigin.LEXICAL,
        RetrievalOrigin.DENSE,
        RetrievalOrigin.LEXICAL,
        RetrievalOrigin.DENSE,
    ]
    assert [candidate.rank for candidate in result.candidates] == [1, 1, 2, 2]
    assert [candidate.selection_rank for candidate in result.candidates] == [1, 2, 3, 4]


def test_selector_keeps_the_first_interleaved_occurrence_of_each_chunk() -> None:
    result = select_m1_context(
        lexical_hits=(
            make_lexical_hit(FIRST_CHUNK_ID, text="first"),
            make_lexical_hit(SECOND_CHUNK_ID, text="duplicate later"),
        ),
        dense_hits=(
            make_dense_hit(SECOND_CHUNK_ID, text="dense first"),
            make_dense_hit(THIRD_CHUNK_ID, text="third"),
        ),
        profile=M1SelectionProfile(),
    )

    assert [candidate.chunk_id for candidate in result.candidates] == [
        FIRST_CHUNK_ID,
        SECOND_CHUNK_ID,
        THIRD_CHUNK_ID,
    ]
    assert result.candidates[1].origin is RetrievalOrigin.DENSE
    assert result.candidates[1].rank == 1


def test_selector_preserves_origin_rank_when_one_lane_is_shorter() -> None:
    result = select_m1_context(
        lexical_hits=(
            make_lexical_hit(FIRST_CHUNK_ID, text="one"),
            make_lexical_hit(SECOND_CHUNK_ID, text="two"),
            make_lexical_hit(THIRD_CHUNK_ID, text="three"),
        ),
        dense_hits=(),
        profile=M1SelectionProfile(),
    )

    assert [candidate.rank for candidate in result.candidates] == [1, 2, 3]
    assert all(candidate.origin is RetrievalOrigin.LEXICAL for candidate in result.candidates)


def test_selector_skips_oversized_chunk_and_considers_later_candidates() -> None:
    result = select_m1_context(
        lexical_hits=(
            make_lexical_hit(FIRST_CHUNK_ID, text="123456"),
            make_lexical_hit(THIRD_CHUNK_ID, text="de"),
        ),
        dense_hits=(make_dense_hit(SECOND_CHUNK_ID, text="abc"),),
        profile=M1SelectionProfile(context_character_limit=5),
    )

    assert [candidate.chunk_id for candidate in result.candidates] == [
        SECOND_CHUNK_ID,
        THIRD_CHUNK_ID,
    ]
    assert [candidate.selection_rank for candidate in result.candidates] == [1, 2]
    assert sum(len(candidate.text) for candidate in result.candidates) == 5


def test_selector_counts_unicode_code_points_without_truncating_text() -> None:
    result = select_m1_context(
        lexical_hits=(make_lexical_hit(FIRST_CHUNK_ID, text="🙂€"),),
        dense_hits=(make_dense_hit(SECOND_CHUNK_ID, text="x"),),
        profile=M1SelectionProfile(context_character_limit=2),
    )

    assert [candidate.text for candidate in result.candidates] == ["🙂€"]


def test_selector_stops_at_the_configured_chunk_limit() -> None:
    result = select_m1_context(
        lexical_hits=(
            make_lexical_hit(FIRST_CHUNK_ID, text="one"),
            make_lexical_hit(THIRD_CHUNK_ID, text="three"),
        ),
        dense_hits=(
            make_dense_hit(SECOND_CHUNK_ID, text="two"),
            make_dense_hit(FOURTH_CHUNK_ID, text="four"),
        ),
        profile=M1SelectionProfile(context_chunk_limit=2),
    )

    assert [candidate.chunk_id for candidate in result.candidates] == [
        FIRST_CHUNK_ID,
        SECOND_CHUNK_ID,
    ]


def test_selector_returns_explicit_empty_result_for_empty_lanes() -> None:
    profile = M1SelectionProfile()

    result = select_m1_context(lexical_hits=(), dense_hits=(), profile=profile)

    assert result.profile is profile
    assert result.candidates == ()


def test_selection_profile_rejects_unknown_algorithm_version() -> None:
    assert M1SelectionProfile().selector_config_version == M1_SELECTOR_CONFIG_VERSION

    with pytest.raises(ValidationError, match="unsupported M1 selector"):
        M1SelectionProfile(selector_config_version="m1-other-selector-v1")
