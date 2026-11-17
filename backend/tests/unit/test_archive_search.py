"""Verify archive ranking and presentation without a database or paid provider."""

from unittest.mock import create_autospec
from uuid import UUID

import pytest

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.identity.application import FixedLocalIdentityContextProvider
from investment_research.modules.search.archive import (
    ArchiveSearchCandidate,
    ArchiveSearchCandidates,
    ArchiveSearchInput,
    ArchiveSearchReader,
    ArchiveSemanticRetrieval,
    SearchArchiveService,
)
from investment_research.modules.search.contracts import (
    DenseQualificationRequest,
    LexicalSearchHit,
    QualifiedDenseHit,
)
from investment_research.modules.search.domain import RetrievalFilters

VERSION = "test-lexical-v1"


def hit(index: int) -> LexicalSearchHit:
    return LexicalSearchHit(
        chunk_id=UUID(int=index),
        document_id=UUID(int=100),
        document_version_id=UUID(int=200),
        structure_block_id=UUID(int=300 + index),
        source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
        text=f"Canonical passage {index}.",
        raw_score=1.0,
    )


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("keyword", [1, 2]),
        ("semantic", [2, 3]),
        ("hybrid", [2, 1, 3]),
    ],
)
def test_service_selects_lane_or_shared_rrf_without_database_ranking(
    mode: str,
    expected: list[int],
) -> None:
    context = FixedLocalIdentityContextProvider().resolve()
    query = ArchiveSearchInput.model_validate(
        {
            "query": '"capacity expansion"',
            "mode": mode,
            "company_id": str(UUID(int=500)),
            "source_attribution": "Grid IR",
            "published_from": "2026-01-01",
            "published_to": "2026-12-31",
        }
    )
    filters = RetrievalFilters(
        company_id=query.company_id,
        source_attribution=query.source_attribution,
        published_from=query.published_from,
        published_to=query.published_to,
    )
    request = DenseQualificationRequest(
        provider_hits=(),
        filters=filters,
        lexical_config_version=VERSION,
        embedding_config_version="test-embedding-v1",
        dense_index_config_version="test-index-v1",
    )
    reader = create_autospec(ArchiveSearchReader, instance=True)
    lexical = tuple(ArchiveSearchCandidate(hit(i), "Synthetic report", None) for i in (1, 2))
    dense = tuple(
        ArchiveSearchCandidate(
            QualifiedDenseHit(**hit(i).model_dump(exclude={"raw_score"}), raw_score=-0.5),
            "Synthetic report",
            None,
        )
        for i in (2, 3)
    )
    reader.read.return_value = ArchiveSearchCandidates(
        lexical=() if mode == "semantic" else lexical,
        dense=() if mode == "keyword" else dense,
        companies=(),
        documents=(),
    )
    semantic = create_autospec(ArchiveSemanticRetrieval, instance=True)
    semantic.retrieve.return_value = request
    result = SearchArchiveService(
        reader=reader,
        semantic=semantic,
        lexical_config_version=VERSION,
    ).search(context, query)

    assert [item.chunk_id.int for item in result.items] == expected
    assert result.has_more is False
    reader.read.assert_called_once()
    args = reader.read.call_args.kwargs
    if mode == "semantic":
        assert args["query"] is None and args["metadata_term"] is None
    else:
        assert args["query"].filters == filters
        assert args["metadata_term"] == "capacity expansion"
    if mode == "keyword":
        semantic.retrieve.assert_not_called()
        assert args["request"] is None
    else:
        semantic.retrieve.assert_called_once_with(
            context,
            text=query.query,
            filters=filters,
            lexical_config_version=VERSION,
        )
        assert args["request"] == request


def test_hybrid_result_limit_is_applied_after_fusing_unique_chunks() -> None:
    context = FixedLocalIdentityContextProvider().resolve()
    reader = create_autospec(ArchiveSearchReader, instance=True)
    reader.read.return_value = ArchiveSearchCandidates(
        lexical=tuple(ArchiveSearchCandidate(hit(i), "Lexical", None) for i in range(1, 22)),
        dense=tuple(
            ArchiveSearchCandidate(QualifiedDenseHit(**hit(i).model_dump()), "Dense", None)
            for i in range(101, 122)
        ),
        companies=(),
        documents=(),
    )
    semantic = create_autospec(ArchiveSemanticRetrieval, instance=True)
    semantic.retrieve.return_value = DenseQualificationRequest(
        provider_hits=(),
        filters=RetrievalFilters(),
        lexical_config_version=VERSION,
        embedding_config_version="test-embedding-v1",
        dense_index_config_version="test-index-v1",
    )
    result = SearchArchiveService(
        reader=reader,
        semantic=semantic,
        lexical_config_version=VERSION,
    ).search(context, ArchiveSearchInput(query="capacity", mode="hybrid"))

    assert result.has_more
    assert len(result.items) == len({item.chunk_id for item in result.items}) == 20
    assert [item.chunk_id.int for item in result.items[:4]] == [1, 101, 2, 102]
