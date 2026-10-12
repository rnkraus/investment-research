"""Versioned input and context contracts for the bounded RRF-plus-BGE experiment."""

from math import isfinite
from typing import Final, Literal

from pydantic import Field, model_validator

from investment_research.modules.search.contracts import (
    M1_CONTEXT_CHARACTER_LIMIT,
    M1_CONTEXT_CHUNK_LIMIT,
    ImmutableSearchContract,
    RerankRequest,
    RerankResponse,
)
from investment_research.modules.search.fusion_contracts import (
    RrfFusedCandidate,
    RrfSelectionProfile,
    RrfSelectionResult,
)
from investment_research.modules.search.limits import MAX_CONTEXT_CHUNKS

BGE_RERANK_MODEL: Final = "bge-reranker-v2-m3"
BGE_RERANK_CONFIG_VERSION: Final = "m1-5-pinecone-bge-none-v1"
BGE_RERANK_CANDIDATE_LIMIT: Final = 40
BGE_RERANK_QUERY_CHARACTER_LIMIT: Final = 1_000
BGE_RERANK_PASSAGE_CHARACTER_LIMIT: Final = 8_000
BGE_RERANK_TOTAL_CHARACTER_LIMIT: Final = 80_000
RRF_RERANK_SELECTOR_VERSION: Final = "m1-5-rrf-k60-bge-none-v1"


def require_bge_input_bounds(request: RerankRequest) -> None:
    """Reject unsupported inputs without truncation, batching, or dropping candidates."""

    if (
        len(request.candidates) > BGE_RERANK_CANDIDATE_LIMIT
        or len(request.normalized_query) > BGE_RERANK_QUERY_CHARACTER_LIMIT
        or any(len(item.text) > BGE_RERANK_PASSAGE_CHARACTER_LIMIT for item in request.candidates)
        or len(request.normalized_query) + sum(len(item.text) for item in request.candidates)
        > BGE_RERANK_TOTAL_CHARACTER_LIMIT
    ):
        raise ValueError("reranking input exceeds the configured limits")


class RrfRerankingProfile(ImmutableSearchContract):
    """Bind this selector to the frozen RRF formula and untruncated BGE configuration."""

    selector_config_version: Literal["m1-5-rrf-k60-bge-none-v1"] = RRF_RERANK_SELECTOR_VERSION
    context_chunk_limit: int = Field(
        default=M1_CONTEXT_CHUNK_LIMIT, ge=1, le=MAX_CONTEXT_CHUNKS, strict=True
    )
    context_character_limit: int = Field(
        default=M1_CONTEXT_CHARACTER_LIMIT, ge=1, le=24_000, strict=True
    )


class PreparedRrfReranking(ImmutableSearchContract):
    """Preserve canonical RRF hits alongside a server-local provider correlation mapping.

    Chunk UUIDs serve only as handles inside the application-owned port, not as persisted
    RetrievalCandidate identifiers. The Pinecone adapter sends texts and correlates positions;
    it never transmits these UUIDs. Preparation does not authorize an external call or evidence.
    """

    profile: RrfRerankingProfile
    fused_candidates: tuple[RrfFusedCandidate, ...] = Field(max_length=BGE_RERANK_CANDIDATE_LIMIT)
    request: RerankRequest | None

    @model_validator(mode="after")
    def require_exact_request_mapping(self) -> PreparedRrfReranking:
        RrfSelectionResult(
            profile=RrfSelectionProfile(), ranked_candidates=self.fused_candidates, candidates=()
        )
        if not self.fused_candidates:
            if self.request is not None:
                raise ValueError("empty fusion must skip the reranking request")
            return self
        if self.request is None:
            raise ValueError("non-empty fusion requires a reranking request")
        if self.request.reranker_config_version != BGE_RERANK_CONFIG_VERSION:
            raise ValueError("unsupported reranking configuration version")
        require_bge_input_bounds(self.request)
        expected = [(item.hit.chunk_id, item.hit.text) for item in self.fused_candidates]
        actual = [(item.retrieval_candidate_id, item.text) for item in self.request.candidates]
        if actual != expected:
            raise ValueError("reranking request must preserve every fused chunk and exact text")
        return self


def require_complete_reranking(
    prepared: PreparedRrfReranking, response: RerankResponse | None
) -> None:
    """Treat scores as untrusted ordering data, never as authorization or source text."""

    if prepared.request is None:
        if response is not None:
            raise ValueError("empty fusion must not receive a provider response")
        return
    if response is None:
        raise ValueError("reranking response is required; fallback is not permitted")
    if (
        response.model != BGE_RERANK_MODEL
        or response.reranker_config_version != BGE_RERANK_CONFIG_VERSION
    ):
        raise ValueError("unexpected reranking model or configuration")
    by_id = {item.hit.chunk_id: item for item in prepared.fused_candidates}
    ids = [item.retrieval_candidate_id for item in response.results]
    if len(ids) != len(by_id) or set(ids) != set(by_id):
        raise ValueError("reranking must return exactly the requested candidate set")
    if [item.rank for item in response.results] != list(range(1, len(ids) + 1)):
        raise ValueError("reranking ranks must be contiguous and ordered")
    if any(not isfinite(item.score) for item in response.results):
        raise ValueError("reranking scores must be finite")
    if list(response.results) != sorted(
        response.results,
        key=lambda item: (-item.score, by_id[item.retrieval_candidate_id].fused_rank),
    ):
        raise ValueError("reranking order must follow scores and original RRF ties")
    if response.usage.input_units < 1 or response.usage.output_units != 0:
        raise ValueError("reranking must report positive input units and zero output units")


class RerankedSelectedCandidate(ImmutableSearchContract):
    """Keep fusion, reranking, and final context positions distinct."""

    candidate: RrfFusedCandidate
    rerank_rank: int = Field(ge=1, le=BGE_RERANK_CANDIDATE_LIMIT, strict=True)
    reranker_score: float = Field(allow_inf_nan=False)
    selection_rank: int = Field(ge=1, le=MAX_CONTEXT_CHUNKS, strict=True)


class RrfRerankingSelectionResult(ImmutableSearchContract):
    """Retain the complete pre/post ranking and provider usage, even for excluded chunks."""

    prepared: PreparedRrfReranking
    response: RerankResponse | None
    candidates: tuple[RerankedSelectedCandidate, ...] = Field(max_length=MAX_CONTEXT_CHUNKS)

    @model_validator(mode="after")
    def require_coherent_context(self) -> RrfRerankingSelectionResult:
        require_complete_reranking(self.prepared, self.response)
        fused_by_id = {item.hit.chunk_id: item for item in self.prepared.fused_candidates}
        scores_by_id = (
            {item.retrieval_candidate_id: item for item in self.response.results}
            if self.response is not None
            else {}
        )
        ids = [item.candidate.hit.chunk_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("reranked context chunks must be unique")
        for item in self.candidates:
            score = scores_by_id.get(item.candidate.hit.chunk_id)
            if (
                fused_by_id.get(item.candidate.hit.chunk_id) != item.candidate
                or score is None
                or score.rank != item.rerank_rank
                or score.score != item.reranker_score
            ):
                raise ValueError("context must retain its canonical candidate and returned score")
        if [item.selection_rank for item in self.candidates] != list(range(1, len(ids) + 1)):
            raise ValueError("context selection ranks must be contiguous and ordered")
        ranks = [item.rerank_rank for item in self.candidates]
        if ranks != sorted(set(ranks)):
            raise ValueError("context must follow reranking order")
        profile = self.prepared.profile
        if (
            len(ids) > profile.context_chunk_limit
            or sum(len(item.candidate.hit.text) for item in self.candidates)
            > profile.context_character_limit
        ):
            raise ValueError("reranked context exceeds the configured limits")
        return self
