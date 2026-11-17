"""Keep informative neighbor expansion bounded, canonical, and date-aware."""

from uuid import UUID

import pytest

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.search.contracts import (
    LexicalSearchHit,
    SelectedRetrievalCandidate,
)
from investment_research.modules.search.domain import RetrievalOrigin
from investment_research.modules.search.text_neighbors import TextNeighbor, select_text_neighbors


def anchor(index: int = 1) -> SelectedRetrievalCandidate:
    return SelectedRetrievalCandidate(
        chunk_id=UUID(int=index),
        document_id=UUID(int=100),
        document_version_id=UUID(int=200),
        structure_block_id=UUID(int=300 + index),
        source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
        page_number=None,
        text=f"The technology joined the platform {index}.",
        raw_score=1.0,
        origin=RetrievalOrigin.DENSE,
        rank=index,
        selection_rank=index,
    )


def neighbor(offset: int, text: str, terms: frozenset[str]) -> TextNeighbor:
    first = anchor()
    return TextNeighbor(
        anchor_id=first.chunk_id,
        offset=offset,
        hit=LexicalSearchHit(
            **first.model_dump(exclude={"origin", "rank", "selection_rank"})
        ).model_copy(
            update={
                "chunk_id": UUID(int=1000 + offset),
                "structure_block_id": UUID(int=2000 + offset),
                "text": text,
            }
        ),
        anchor_terms=frozenset({"technolog", "platform"}),
        neighbor_terms=terms,
        query_terms=frozenset({"technolog", "platform", "bottleneck", "time"}),
    )


@pytest.mark.parametrize("direction", [1, -1])
def test_non_how_question_recovers_informative_neighbor_and_intervening_block(
    direction: int,
) -> None:
    selected = tuple(anchor(index) for index in range(1, 9))
    middle = neighbor(direction, "Cold starts leave GPUs idle.", frozenset({"cold", "gpu", "idl"}))
    endpoint = neighbor(
        2 * direction,
        "The technology cuts this idle GPU tax, enabling improved utilization.",
        frozenset({"technolog", "gpu", "idl", "enabl", "time"}),
    )
    result, added = select_text_neighbors(
        "Which platform did the technology join, and what bottleneck did it reduce?",
        selected,
        (middle, endpoint),
        chunk_limit=8,
        character_limit=24000,
    )
    expected = (middle.hit, endpoint.hit) if direction == 1 else (endpoint.hit, middle.hit)
    expected_ids = [hit.chunk_id for hit in expected]
    assert [hit.chunk_id for hit in added] == expected_ids
    window = (
        [selected[0].chunk_id, *expected_ids]
        if direction == 1
        else [*expected_ids, selected[0].chunk_id]
    )
    assert [hit.chunk_id for hit in result[:3]] == window
    assert {hit.chunk_id for hit in selected[:4]} <= {hit.chunk_id for hit in result}
    assert len(result) == 8
    assert [hit.selection_rank for hit in result] == list(range(1, 9))
    assert [hit.rank for hit in result if hit.origin.value == "STRUCTURED"] == [1, 2]


@pytest.mark.parametrize(
    "failure",
    ["foreign_version", "foreign_document", "distant", "duplicate", "wrong_date", "budget"],
)
def test_neighbor_selection_rejects_ineligible_or_unfitting_text(failure: str) -> None:
    first = anchor()
    item = neighbor(
        1, "The technology enables utilization.", frozenset({"technolog", "enabl", "time"})
    )
    question = "Which production bottleneck does the technology reduce?"
    budget = 24000
    if failure == "foreign_version":
        item = TextNeighbor(
            item.anchor_id,
            item.offset,
            item.hit.model_copy(update={"document_version_id": UUID(int=900)}),
            item.anchor_terms,
            item.neighbor_terms,
            item.query_terms,
        )
    elif failure == "foreign_document":
        item = TextNeighbor(
            item.anchor_id,
            item.offset,
            item.hit.model_copy(update={"document_id": UUID(int=900)}),
            item.anchor_terms,
            item.neighbor_terms,
            item.query_terms,
        )
    elif failure == "distant":
        item = TextNeighbor(
            item.anchor_id, 3, item.hit, item.anchor_terms, item.neighbor_terms, item.query_terms
        )
    elif failure == "duplicate":
        item = TextNeighbor(
            item.anchor_id,
            item.offset,
            item.hit.model_copy(update={"text": first.text}),
            item.anchor_terms,
            item.neighbor_terms,
            item.query_terms,
        )
    elif failure == "wrong_date":
        question += " as of November 6, 2026?"
    else:
        budget = len(first.text)
    assert select_text_neighbors(
        question,
        (first,),
        (item,),
        chunk_limit=8,
        character_limit=budget,
    ) == ((first,), ())


def test_single_neighbor_and_dated_intervening_paragraph_are_checked_separately() -> None:
    first = anchor()
    middle = neighbor(
        1,
        "September 30, 2026: production GPUs were idle.",
        frozenset({"gpu", "idl", "production"}),
    )
    endpoint = neighbor(
        2,
        "November 6, 2026: technology enables lower idle GPU time.",
        frozenset({"technolog", "gpu", "idl", "enabl", "time"}),
    )
    result, added = select_text_neighbors(
        "Which production bottleneck does the technology reduce as of November 6, 2026?",
        (first,),
        (middle, endpoint),
        chunk_limit=8,
        character_limit=24000,
    )
    assert [hit.chunk_id for hit in result] == [first.chunk_id, endpoint.hit.chunk_id]
    assert added == (endpoint.hit,)
