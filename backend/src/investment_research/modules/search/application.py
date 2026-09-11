"""Application helpers for deterministic bounded lexical retrieval."""

from unicodedata import normalize

from pydantic import TypeAdapter

from investment_research.modules.search.contracts import LexicalSearchQuery
from investment_research.modules.search.domain import (
    ConfigurationVersion,
    NormalizedQuery,
    QueryText,
    RetrievalFilters,
)

QUERY_TEXT_ADAPTER = TypeAdapter(QueryText)
NORMALIZED_QUERY_ADAPTER = TypeAdapter(NormalizedQuery)


def normalize_lexical_query(query_text: str) -> str:
    """Normalize Unicode, case, and whitespace without retaining a second raw question."""

    validated_query = QUERY_TEXT_ADAPTER.validate_python(query_text)
    canonical_unicode = normalize("NFKC", validated_query)
    collapsed_whitespace = " ".join(canonical_unicode.split())
    return NORMALIZED_QUERY_ADAPTER.validate_python(collapsed_whitespace.casefold())


def build_lexical_search_query(
    *,
    query_text: str,
    filters: RetrievalFilters,
    lexical_config_version: ConfigurationVersion,
    candidate_limit: int,
) -> LexicalSearchQuery:
    """Build the complete typed query passed to a lexical-search adapter."""

    return LexicalSearchQuery(
        normalized_query=normalize_lexical_query(query_text),
        filters=filters,
        lexical_config_version=lexical_config_version,
        candidate_limit=candidate_limit,
    )
