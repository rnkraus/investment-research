"""Read-only discovery of canonical passages without creating research requests."""

from typing import Annotated, Protocol, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from investment_research.modules.documents.domain import DocumentTitle, SourceLocatorKind
from investment_research.modules.documents.publication import PublicationDateBounds
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.application import normalize_lexical_query
from investment_research.modules.search.contracts import (
    ImmutableSearchContract,
    LexicalSearchQuery,
)
from investment_research.modules.search.domain import ConfigurationVersion, RetrievalFilters

ARCHIVE_SEARCH_LIMIT = 20
ARCHIVE_PASSAGE_LIMIT = 4_000


class ArchiveSearchInput(ImmutableSearchContract, PublicationDateBounds):
    """One lexical query with optional company and publication filters."""

    query: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=300, pattern=r"^[^\x00]+$"
        ),
    ]
    company_id: UUID | None = None

    @field_validator("query")
    @classmethod
    def normalize_query(cls, query: str) -> str:
        return normalize_lexical_query(query)


class ArchiveSearchHit(ImmutableSearchContract):
    """A passage preview and the identifiers needed to open its current original."""

    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    title: DocumentTitle
    published_at: AwareDatetime | None
    source_locator_kind: SourceLocatorKind
    page_number: int | None = Field(ge=1)
    passage: str = Field(min_length=1, max_length=ARCHIVE_PASSAGE_LIMIT)

    @model_validator(mode="after")
    def require_media_appropriate_locator(self) -> Self:
        if (self.source_locator_kind is SourceLocatorKind.PDF_PAGE) != (
            self.page_number is not None
        ):
            raise ValueError("only PDF hits have a physical page number")
        return self


class ArchiveSearchResult(ImmutableSearchContract):
    """A bounded ranked result set with an indication that matches remain."""

    items: tuple[ArchiveSearchHit, ...] = Field(max_length=ARCHIVE_SEARCH_LIMIT)
    has_more: bool


class ArchiveSearchReader(Protocol):
    def search(
        self, context: WorkspaceContext, query: LexicalSearchQuery
    ) -> tuple[ArchiveSearchHit, ...]:
        """Return current eligible passages with display metadata in lexical rank order."""


class SearchArchiveService:
    def __init__(
        self, *, reader: ArchiveSearchReader, lexical_config_version: ConfigurationVersion
    ) -> None:
        self._reader = reader
        self._lexical_config_version = lexical_config_version

    def search(self, context: WorkspaceContext, command: ArchiveSearchInput) -> ArchiveSearchResult:
        query = LexicalSearchQuery(
            normalized_query=command.query,
            filters=RetrievalFilters(
                company_id=command.company_id,
                published_from=command.published_from,
                published_to=command.published_to,
            ),
            lexical_config_version=self._lexical_config_version,
            candidate_limit=ARCHIVE_SEARCH_LIMIT + 1,
        )
        hits = self._reader.search(context, query)
        return ArchiveSearchResult(
            items=hits[:ARCHIVE_SEARCH_LIMIT], has_more=len(hits) > ARCHIVE_SEARCH_LIMIT
        )
