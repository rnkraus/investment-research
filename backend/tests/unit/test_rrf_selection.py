"""Offline regressions for opt-in M1.5 rank fusion and bounded context selection."""

from fractions import Fraction
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.search.contracts import LexicalSearchHit, QualifiedDenseHit
from investment_research.modules.search.domain import RetrievalOrigin
from investment_research.modules.search.fusion import fuse_rrf_hits, select_rrf_context
from investment_research.modules.search.fusion_contracts import (
    RRF_CONFIG_VERSION,
    RrfFusedCandidate,
    RrfSelectionProfile,
    RrfSelectionResult,
)


def lexical(number: int, *, text: str = "canonical text", score: float = 0.1) -> LexicalSearchHit:
    return LexicalSearchHit(
        chunk_id=UUID(int=number),
        document_id=UUID(int=101),
        document_version_id=UUID(int=102),
        structure_block_id=UUID(int=103),
        source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
        text=text,
        raw_score=score,
    )


def dense(number: int, *, text: str = "canonical text", score: float = -0.5) -> QualifiedDenseHit:
    return QualifiedDenseHit.model_validate(
        lexical(number, text=text).model_dump() | {"raw_score": score}
    )


def test_shared_chunk_accumulates_both_votes_and_preserves_each_lane_record() -> None:
    hits = fuse_rrf_hits(
        lexical_hits=(lexical(1), lexical(2, score=9.5)),
        dense_hits=(dense(2, score=-0.9), dense(3)),
    )

    assert [item.hit.chunk_id.int for item in hits] == [2, 1, 3]
    shared = hits[0]
    assert shared.fused_rank == 1
    assert shared.fused_score == float(Fraction(1, 62) + Fraction(1, 61))
    assert [(item.origin, item.rank, item.raw_score) for item in shared.contributions] == [
        (RetrievalOrigin.LEXICAL, 2, 9.5),
        (RetrievalOrigin.DENSE, 1, -0.9),
    ]
    assert shared.hit.raw_score == 9.5


@pytest.mark.parametrize("lane", ["lexical", "dense"])
def test_single_lane_preserves_rank_without_sorting_raw_scores(lane: str) -> None:
    result = fuse_rrf_hits(
        lexical_hits=(lexical(2, score=0.01), lexical(1, score=99)) if lane == "lexical" else (),
        dense_hits=(dense(2, score=-0.9), dense(1, score=0.9)) if lane == "dense" else (),
    )

    assert [item.hit.chunk_id.int for item in result] == [2, 1]
    assert [item.fused_rank for item in result] == [1, 2]
    assert [item.fused_score for item in result] == [1 / 61, 1 / 62]


def test_equal_cross_lane_rank_sums_use_chunk_id_not_lane_or_raw_score() -> None:
    result = fuse_rrf_hits(
        lexical_hits=(lexical(2, score=100), lexical(1, score=0.01)),
        dense_hits=(dense(1, score=-1), dense(2, score=1)),
    )

    assert result[0].fused_score == result[1].fused_score
    assert [item.hit.chunk_id.int for item in result] == [1, 2]


def test_equal_single_lane_contributions_do_not_prefer_lexical() -> None:
    result = fuse_rrf_hits(lexical_hits=(lexical(2),), dense_hits=(dense(1),))

    assert [item.hit.chunk_id.int for item in result] == [1, 2]


def test_overlap_at_rank_twenty_beats_single_lane_rank_one() -> None:
    result = fuse_rrf_hits(
        lexical_hits=tuple(lexical(number) for number in range(1, 21)),
        dense_hits=tuple(dense(number) for number in (*range(21, 40), 20)),
    )

    assert result[0].hit.chunk_id.int == 20
    assert result[0].fused_score == 2 / 80


def test_complete_union_is_retained_before_context_selection() -> None:
    result = fuse_rrf_hits(
        lexical_hits=tuple(lexical(number) for number in range(1, 21)),
        dense_hits=tuple(dense(number) for number in range(21, 41)),
    )

    assert len(result) == 40
    assert [item.fused_rank for item in result] == list(range(1, 41))


@pytest.mark.parametrize("lane", ["lexical", "dense"])
def test_duplicate_chunk_within_a_lane_fails_instead_of_counting_extra_votes(lane: str) -> None:
    with pytest.raises(ValueError, match="duplicate chunks"):
        fuse_rrf_hits(
            lexical_hits=(lexical(1), lexical(1)) if lane == "lexical" else (),
            dense_hits=(dense(1), dense(1)) if lane == "dense" else (),
        )


@pytest.mark.parametrize(
    "field", ["text", "document_id", "document_version_id", "structure_block_id"]
)
def test_shared_chunk_with_disagreeing_canonical_state_fails(field: str) -> None:
    changed = dense(1).model_copy(update={field: "different" if field == "text" else UUID(int=999)})

    with pytest.raises(ValueError, match="disagree about canonical"):
        fuse_rrf_hits(lexical_hits=(lexical(1),), dense_hits=(changed,))


def test_shared_chunk_with_disagreeing_locator_fails() -> None:
    changed = dense(1).model_copy(
        update={"source_locator_kind": SourceLocatorKind.PDF_PAGE, "page_number": 1}
    )

    with pytest.raises(ValueError, match="disagree about canonical"):
        fuse_rrf_hits(lexical_hits=(lexical(1),), dense_hits=(changed,))


@pytest.mark.parametrize("lane", ["lexical", "dense"])
def test_lane_limit_fails_without_silently_truncating_inputs(lane: str) -> None:
    with pytest.raises(ValueError, match="candidate limit"):
        fuse_rrf_hits(
            lexical_hits=tuple(lexical(n) for n in range(1, 22)) if lane == "lexical" else (),
            dense_hits=tuple(dense(n) for n in range(1, 22)) if lane == "dense" else (),
        )


def test_raw_unqualified_object_is_not_a_dense_fusion_input() -> None:
    with pytest.raises(TypeError, match="canonical hits"):
        fuse_rrf_hits(lexical_hits=(), dense_hits=(cast(QualifiedDenseHit, object()),))


def test_wrong_lane_contract_is_rejected_at_runtime() -> None:
    with pytest.raises(TypeError, match="declared lane"):
        fuse_rrf_hits(lexical_hits=(cast(LexicalSearchHit, dense(1)),), dense_hits=())


def test_context_limits_skip_whole_chunks_and_keep_fusion_rank_separate() -> None:
    result = select_rrf_context(
        lexical_hits=(lexical(1, text="123456"), lexical(2, text="abc"), lexical(3, text="de")),
        dense_hits=(),
        profile=RrfSelectionProfile(context_character_limit=5),
    )

    assert [item.candidate.hit.text for item in result.candidates] == ["abc", "de"]
    assert [item.candidate.fused_rank for item in result.candidates] == [2, 3]
    assert [item.selection_rank for item in result.candidates] == [1, 2]
    assert [item.fused_rank for item in result.ranked_candidates] == [1, 2, 3]


def test_context_counts_unicode_code_points_and_does_not_rewrite_source_text() -> None:
    result = select_rrf_context(
        lexical_hits=(lexical(1, text="🙂€\n"), lexical(2, text="x")),
        dense_hits=(),
        profile=RrfSelectionProfile(context_character_limit=3),
    )

    assert [item.candidate.hit.text for item in result.candidates] == ["🙂€\n"]


def test_context_stops_at_default_eight_chunks() -> None:
    result = select_rrf_context(
        lexical_hits=tuple(lexical(number) for number in range(1, 21)),
        dense_hits=(),
        profile=RrfSelectionProfile(),
    )

    assert len(result.candidates) == 8


def test_context_honors_lower_chunk_limit_and_deduplicates_shared_chunks() -> None:
    result = select_rrf_context(
        lexical_hits=(lexical(1), lexical(2)),
        dense_hits=(dense(1), dense(3)),
        profile=RrfSelectionProfile(context_chunk_limit=2),
    )

    assert [item.candidate.hit.chunk_id.int for item in result.candidates] == [1, 2]


def test_tuned_context_can_exceed_eight_without_exceeding_text_budget() -> None:
    result = select_rrf_context(
        lexical_hits=tuple(lexical(number, text="source") for number in range(1, 21)),
        dense_hits=tuple(dense(number, text="source") for number in range(21, 41)),
        profile=RrfSelectionProfile(context_chunk_limit=32, context_character_limit=192),
    )
    assert len(result.candidates) == 32
    assert [item.selection_rank for item in result.candidates] == list(range(1, 33))
    assert sum(len(item.candidate.hit.text) for item in result.candidates) == 192


def test_empty_lanes_return_explicit_empty_fusion_and_context() -> None:
    assert fuse_rrf_hits(lexical_hits=(), dense_hits=()) == ()
    profile = RrfSelectionProfile()
    result = select_rrf_context(lexical_hits=(), dense_hits=(), profile=profile)
    assert result.profile is profile
    assert result.candidates == ()
    assert result.ranked_candidates == ()


@pytest.mark.parametrize(
    "values",
    [
        {"selector_config_version": "other-rrf"},
        {"rank_constant": 61},
        {"context_chunk_limit": 0},
        {"context_chunk_limit": 33},
        {"context_chunk_limit": True},
        {"context_character_limit": 0},
        {"context_character_limit": 24_001},
    ],
)
def test_profile_rejects_unversioned_formula_changes_and_invalid_bounds(
    values: dict[str, object],
) -> None:
    assert RrfSelectionProfile().selector_config_version == RRF_CONFIG_VERSION
    with pytest.raises(ValidationError):
        RrfSelectionProfile.model_validate(values)


def test_fused_contract_rejects_manipulated_score_and_duplicate_origins() -> None:
    candidate = fuse_rrf_hits(lexical_hits=(lexical(1),), dense_hits=())[0]
    with pytest.raises(ValidationError, match="score does not match"):
        RrfFusedCandidate.model_validate(candidate.model_dump() | {"fused_score": 0.9})
    with pytest.raises(ValidationError, match="unique origins"):
        RrfFusedCandidate.model_validate(
            candidate.model_dump() | {"contributions": candidate.contributions * 2}
        )


def test_result_contract_rejects_forged_context_budget() -> None:
    result = select_rrf_context(
        lexical_hits=(lexical(1, text="abc"), lexical(2, text="de")),
        dense_hits=(),
        profile=RrfSelectionProfile(),
    )
    with pytest.raises(ValidationError, match="character limit"):
        RrfSelectionResult.model_validate(
            result.model_dump() | {"profile": RrfSelectionProfile(context_character_limit=4)}
        )
    with pytest.raises(ValidationError, match="chunk limit"):
        RrfSelectionResult.model_validate(
            result.model_dump() | {"profile": RrfSelectionProfile(context_chunk_limit=1)}
        )


def test_result_contract_rejects_selection_not_in_full_ranking() -> None:
    result = select_rrf_context(
        lexical_hits=(lexical(1), lexical(2)), dense_hits=(), profile=RrfSelectionProfile()
    )
    with pytest.raises(ValidationError, match="original ranked candidate"):
        RrfSelectionResult.model_validate(
            result.model_dump() | {"ranked_candidates": result.ranked_candidates[:1]}
        )


def test_result_contract_rejects_duplicate_or_reordered_full_ranking() -> None:
    result = select_rrf_context(
        lexical_hits=(lexical(1), lexical(2)), dense_hits=(), profile=RrfSelectionProfile()
    )
    with pytest.raises(ValidationError, match="ranked chunks must be unique"):
        RrfSelectionResult.model_validate(
            result.model_dump() | {"ranked_candidates": result.ranked_candidates[:1] * 2}
        )
    with pytest.raises(ValidationError, match="fusion ranks must be contiguous"):
        RrfSelectionResult.model_validate(
            result.model_dump() | {"ranked_candidates": result.ranked_candidates[::-1]}
        )
