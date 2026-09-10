"""Application-owned parsing and persistence contracts for document processing."""

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import BinaryIO, Protocol
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.contracts import ParsedDocument, ParserLimits
from investment_research.modules.processing.domain import (
    Chunk,
    ChunkSet,
    ConfigurationVersion,
    ProcessingRun,
    StructureBlock,
)


class DocumentParserError(Exception):
    """Base error for expected, safely reportable parser failures."""


class ParserLimitExceededError(DocumentParserError):
    """Raised when parsing would exceed an application-owned safety limit."""


class ProcessingExecutionLimitError(Exception):
    """Raised when the outer runner reaches a process-level execution limit."""


class DocumentParser(Protocol):
    """Extract ordered page text from one PDF through an interchangeable adapter."""

    @property
    def parser_version(self) -> ConfigurationVersion:
        """Return the stable implementation version recorded on processing runs."""

    def parse(self, content: BinaryIO, *, limits: ParserLimits) -> ParsedDocument:
        """Parse one binary stream without closing its caller-owned resource."""


class ProcessingOriginalProvider(Protocol):
    """Open one canonical original without exposing document persistence details."""

    def open_for_processing(
        self,
        context: WorkspaceContext,
        document_version_id: UUID,
    ) -> AbstractContextManager[BinaryIO]:
        """Yield verified bytes for one eligible version and close them afterward."""


class ProcessingRepository(Protocol):
    """Persist processing state and derived lexical content within one workspace."""

    def add_run(self, context: WorkspaceContext, run: ProcessingRun) -> None:
        """Stage a newly captured processing run."""

    def get_run_by_id(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> ProcessingRun | None:
        """Return a scoped run without disclosing inaccessible identifiers."""

    def get_run_by_id_for_update(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> ProcessingRun | None:
        """Lock and return a scoped run for one atomic lifecycle transition."""

    def update_run(self, context: WorkspaceContext, run: ProcessingRun) -> bool:
        """Update a scoped run snapshot and report whether it existed."""

    def add_lexical_output(
        self,
        context: WorkspaceContext,
        blocks: Sequence[StructureBlock],
        chunk_set: ChunkSet,
        chunks: Sequence[Chunk],
    ) -> None:
        """Stage one internally consistent set of parsed blocks and lexical chunks."""


class ProcessingUnitOfWork(Protocol):
    """Processing repository participating in one short database transaction."""

    @property
    def processing(self) -> ProcessingRepository:
        """Return the repository used for one processing state change."""


type ProcessingUnitOfWorkFactory = Callable[
    [],
    AbstractContextManager[ProcessingUnitOfWork],
]


class ProcessingExecutor(Protocol):
    """Execute one canonical processing run outside the API process."""

    def execute(self, processing_run_id: UUID) -> ProcessingRun:
        """Resolve trusted scope and process exactly one run identifier."""
