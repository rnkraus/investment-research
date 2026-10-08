"""Offline RRF-plus-rerank selection tests, without provider execution or persistence."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.search.contracts import (
    LexicalSearchHit,
    QualifiedDenseHit,
    RerankResponse,
    RerankResult,
)
from investment_research.modules.search.fusion import select_rrf_context
from investment_research.modules.search.fusion_contracts import RrfSelectionProfile
from investment_research.modules.search.ports import SearchProviderContractError
from investment_research.modules.search.reranking import (
    prepare_rrf_reranking,
    select_reranked_context,
)
from investment_research.modules.search.reranking_contracts import (
    BGE_RERANK_CONFIG_VERSION,
    BGE_RERANK_MODEL,
    PreparedRrfReranking,
    RrfRerankingProfile,
    RrfRerankingSelectionResult,
)


def lexical(number: int, *, text: str = "Synthetic canonical text.") -> LexicalSearchHit:
    return LexicalSearchHit(
        chunk_id=UUID(int=number),
        document_id=UUID(int=101),
        document_version_id=UUID(int=102),
        structure_block_id=UUID(int=103),
        source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
        text=text,
        raw_score=0.1,
    )


def prepare(
    *,
    texts: tuple[str, ...] = ("first", "second", "third"),
    profile: RrfRerankingProfile | None = None,
) -> PreparedRrfReranking:
    return prepare_rrf_reranking(
        normalized_query="what constrained delivery?",
        lexical_hits=tuple(lexical(index, text=text) for index, text in enumerate(texts, 1)),
        dense_hits=(),
        profile=profile or RrfRerankingProfile(),
    )


def response(order: tuple[int, ...] = (3, 1, 2)) -> RerankResponse:
    return RerankResponse(
        results=tuple(
            RerankResult(retrieval_candidate_id=UUID(int=number), rank=rank, score=1 / rank)
            for rank, number in enumerate(order, 1)
        ),
        usage=ProviderUsage(input_units=1, output_units=0),
        model=BGE_RERANK_MODEL,
        reranker_config_version=BGE_RERANK_CONFIG_VERSION,
    )


def test_preparation_deduplicates_cross_lane_hits_but_preserves_both_votes() -> None:
    hit = lexical(1, text="🙂€\nexact  spacing")
    dense = QualifiedDenseHit.model_validate(hit.model_dump() | {"raw_score": 0.9})
    prepared = prepare_rrf_reranking(
        normalized_query="query",
        lexical_hits=(hit, lexical(2)),
        dense_hits=(dense,),
        profile=RrfRerankingProfile(),
    )

    assert prepared.request is not None
    assert len(prepared.request.candidates) == 2
    assert prepared.request.candidates[0].text == hit.text
    assert prepared.request.candidates[0].retrieval_candidate_id == hit.chunk_id
    assert len(prepared.fused_candidates[0].contributions) == 2


def test_preparation_retains_the_complete_forty_hit_union() -> None:
    prepared = prepare_rrf_reranking(
        normalized_query="query",
        lexical_hits=tuple(lexical(n) for n in range(1, 21)),
        dense_hits=tuple(
            QualifiedDenseHit.model_validate(lexical(n).model_dump()) for n in range(21, 41)
        ),
        profile=RrfRerankingProfile(),
    )
    assert prepared.request is not None
    assert len(prepared.request.candidates) == len(prepared.fused_candidates) == 40


@pytest.mark.parametrize("kind", ["query", "passage", "total"])
def test_preparation_rejects_over_limit_inputs_without_shortening(kind: str) -> None:
    texts = ("x" * 8_001,) if kind == "passage" else ("x" * 8_000,) * 10
    with pytest.raises(SearchProviderContractError, match="input contract"):
        prepare_rrf_reranking(
            normalized_query="q" * 1_001 if kind == "query" else "q",
            lexical_hits=tuple(lexical(n, text=text) for n, text in enumerate(texts, 1)),
            dense_hits=(),
            profile=RrfRerankingProfile(),
        )


def test_exact_input_character_boundaries_are_accepted() -> None:
    prepared = prepare_rrf_reranking(
        normalized_query="q" * 1_000,
        lexical_hits=(
            *(lexical(n, text="x" * 8_000) for n in range(1, 10)),
            lexical(10, text="x" * 7_000),
        ),
        dense_hits=(),
        profile=RrfRerankingProfile(),
    )
    assert prepared.request is not None
    assert sum(len(item.text) for item in prepared.request.candidates) + 1_000 == 80_000


def test_empty_fusion_skips_request_response_and_usage() -> None:
    prepared = prepare(texts=())
    result = select_reranked_context(prepared=prepared, response=None)
    assert result.prepared.request is None
    assert result.response is None
    assert result.candidates == ()
    with pytest.raises(SearchProviderContractError):
        select_reranked_context(prepared=prepared, response=response())


def test_missing_response_is_not_a_silent_rrf_fallback() -> None:
    with pytest.raises(SearchProviderContractError, match="selection contract"):
        select_reranked_context(prepared=prepare(), response=None)


def test_reranking_changes_context_order_without_changing_rrf_or_sources() -> None:
    prepared = prepare()
    provider_response = response()
    result = select_reranked_context(prepared=prepared, response=provider_response)

    assert [item.candidate.hit.chunk_id.int for item in result.candidates] == [3, 1, 2]
    assert [item.candidate.fused_rank for item in result.candidates] == [3, 1, 2]
    assert [item.rerank_rank for item in result.candidates] == [1, 2, 3]
    assert [item.selection_rank for item in result.candidates] == [1, 2, 3]
    assert [item.candidate.hit.text for item in result.candidates] == ["third", "first", "second"]
    assert result.response is provider_response
    assert result.prepared is prepared
    assert [item.fused_rank for item in prepared.fused_candidates] == [1, 2, 3]


def test_context_skips_whole_oversized_chunks_and_keeps_full_ranking_and_usage() -> None:
    prepared = prepare(
        texts=("🙂€\n", "xy", "too long"),
        profile=RrfRerankingProfile(context_character_limit=5),
    )
    result = select_reranked_context(prepared=prepared, response=response())
    assert [item.candidate.hit.text for item in result.candidates] == ["🙂€\n", "xy"]
    assert [item.rerank_rank for item in result.candidates] == [2, 3]
    assert [item.selection_rank for item in result.candidates] == [1, 2]
    assert result.response is not None
    assert len(result.response.results) == 3
    assert result.response.usage.input_units == 1


@pytest.mark.parametrize("limit", [1, 8])
def test_context_honors_chunk_limit(limit: int) -> None:
    prepared = prepare(texts=("text",) * 10, profile=RrfRerankingProfile(context_chunk_limit=limit))
    result = select_reranked_context(prepared=prepared, response=response(tuple(range(10, 0, -1))))
    assert len(result.candidates) == limit
    assert result.candidates[0].candidate.hit.chunk_id.int == 10


def test_equal_scores_use_original_rrf_order() -> None:
    tied = response((1, 2, 3)).model_copy(
        update={
            "results": tuple(
                item.model_copy(update={"score": -0.25}) for item in response((1, 2, 3)).results
            )
        }
    )
    result = select_reranked_context(prepared=prepare(), response=tied)
    assert [item.candidate.fused_rank for item in result.candidates] == [1, 2, 3]


@pytest.mark.parametrize(
    "kind",
    ["missing", "foreign", "duplicate", "rank", "score", "tie", "nan", "model", "config", "usage"],
)
def test_untrusted_response_fails_without_fallback(kind: str) -> None:
    original = response()
    results = original.results
    updates: dict[str, object] = {}
    if kind == "missing":
        results = results[:-1]
    elif kind in {"foreign", "duplicate"}:
        results = (
            results[0].model_copy(
                update={
                    "retrieval_candidate_id": UUID(int=999)
                    if kind == "foreign"
                    else results[1].retrieval_candidate_id
                }
            ),
            *results[1:],
        )
    elif kind == "rank":
        results = (results[0].model_copy(update={"rank": 2}), *results[1:])
    elif kind in {"score", "nan"}:
        results = (
            results[0].model_copy(update={"score": -1.0 if kind == "score" else float("nan")}),
            *results[1:],
        )
    elif kind == "tie":
        results = tuple(item.model_copy(update={"score": 0.5}) for item in results)
    elif kind == "model":
        updates["model"] = "other-model"
    elif kind == "config":
        updates["reranker_config_version"] = "other-config"
    else:
        updates["usage"] = ProviderUsage(input_units=0, output_units=1)
    forged = original.model_copy(update=updates | {"results": results})
    with pytest.raises(SearchProviderContractError, match="selection contract"):
        select_reranked_context(prepared=prepare(), response=forged)


@pytest.mark.parametrize("kind", ["text", "id", "order", "config", "missing"])
def test_prepared_contract_rejects_changed_request_mapping(kind: str) -> None:
    prepared = prepare()
    request = prepared.request
    assert request is not None
    values = request.model_dump()
    if kind == "text":
        values["candidates"][0]["text"] = "changed"
    elif kind == "id":
        values["candidates"][0]["retrieval_candidate_id"] = UUID(int=999)
    elif kind == "order":
        values["candidates"] = values["candidates"][::-1]
    elif kind == "config":
        values["reranker_config_version"] = "other-config"
    with pytest.raises(ValidationError):
        PreparedRrfReranking.model_validate(
            prepared.model_dump() | {"request": None if kind == "missing" else values}
        )


@pytest.mark.parametrize("kind", ["source", "score", "rerank_rank", "selection_rank", "budget"])
def test_result_contract_rejects_forged_selection(kind: str) -> None:
    result = select_reranked_context(prepared=prepare(), response=response())
    values = result.model_dump()
    item = values["candidates"][0]
    if kind == "source":
        item["candidate"]["hit"]["text"] = "changed"
    elif kind == "score":
        item["reranker_score"] = 0.01
    elif kind == "budget":
        values["prepared"]["profile"]["context_character_limit"] = 1
    else:
        item[kind] = 2
    with pytest.raises(ValidationError):
        RrfRerankingSelectionResult.model_validate(values)


@pytest.mark.parametrize(
    "values",
    [
        {"selector_config_version": "other"},
        {"context_chunk_limit": 9},
        {"context_chunk_limit": True},
        {"context_character_limit": 24_001},
    ],
)
def test_profile_rejects_unversioned_or_unbounded_selection(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RrfRerankingProfile.model_validate(values)


def test_explicit_no_rerank_rrf_path_is_unchanged() -> None:
    hits = (lexical(1), lexical(2), lexical(3))
    result = select_rrf_context(lexical_hits=hits, dense_hits=(), profile=RrfSelectionProfile())
    assert [item.candidate.hit.chunk_id.int for item in result.candidates] == [1, 2, 3]
    assert result.profile.selector_config_version == "m1-5-rrf-k60-v1"
