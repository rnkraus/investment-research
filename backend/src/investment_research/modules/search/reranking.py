"""Pure preparation and context selection; provider execution belongs to the caller."""

from collections.abc import Sequence

from pydantic import ValidationError

from investment_research.modules.search.contracts import (
    LexicalSearchHit,
    QualifiedDenseHit,
    RerankCandidate,
    RerankRequest,
    RerankResponse,
)
from investment_research.modules.search.fusion import fuse_rrf_hits
from investment_research.modules.search.ports import SearchProviderContractError
from investment_research.modules.search.reranking_contracts import (
    BGE_RERANK_CONFIG_VERSION,
    PreparedRrfReranking,
    RerankedSelectedCandidate,
    RrfRerankingProfile,
    RrfRerankingSelectionResult,
    require_complete_reranking,
)


def prepare_rrf_reranking(
    *,
    normalized_query: str,
    lexical_hits: Sequence[LexicalSearchHit],
    dense_hits: Sequence[QualifiedDenseHit],
    profile: RrfRerankingProfile,
) -> PreparedRrfReranking:
    """Deduplicate eligible hits before any call; an empty union needs no provider request.

    The caller must establish workspace, version, time, and public/synthetic eligibility.
    Reserve usage/quota and execute the port outside a transaction only after preparation.
    """

    fused = fuse_rrf_hits(lexical_hits=lexical_hits, dense_hits=dense_hits)
    try:
        request = (
            RerankRequest(
                normalized_query=normalized_query,
                candidates=tuple(
                    RerankCandidate(retrieval_candidate_id=item.hit.chunk_id, text=item.hit.text)
                    for item in fused
                ),
                reranker_config_version=BGE_RERANK_CONFIG_VERSION,
            )
            if fused
            else None
        )
        return PreparedRrfReranking(profile=profile, fused_candidates=fused, request=request)
    except ValidationError:
        raise SearchProviderContractError(
            "reranking preparation violates its input contract"
        ) from None


def select_reranked_context(
    *, prepared: PreparedRrfReranking, response: RerankResponse | None
) -> RrfRerankingSelectionResult:
    """Apply validated relevance order under unchanged whole-chunk generation limits.

    No fallback, provider call, persistence, or evidence authorization occurs here. The caller
    must revalidate canonical eligibility before storing or using the resulting snapshot.
    """

    try:
        require_complete_reranking(prepared, response)
        by_id = {item.hit.chunk_id: item for item in prepared.fused_candidates}
        selected: list[RerankedSelectedCandidate] = []
        remaining = prepared.profile.context_character_limit
        for score in response.results if response is not None else ():
            candidate = by_id[score.retrieval_candidate_id]
            if len(selected) == prepared.profile.context_chunk_limit:
                break
            if len(candidate.hit.text) > remaining:
                continue
            selected.append(
                RerankedSelectedCandidate(
                    candidate=candidate,
                    rerank_rank=score.rank,
                    reranker_score=score.score,
                    selection_rank=len(selected) + 1,
                )
            )
            remaining -= len(candidate.hit.text)
        return RrfRerankingSelectionResult(
            prepared=prepared, response=response, candidates=tuple(selected)
        )
    except ValueError:
        raise SearchProviderContractError(
            "reranking response violates its selection contract"
        ) from None
