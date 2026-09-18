"""Application-owned parsing and persistence contracts for document processing."""

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Protocol
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.contracts import (
    ParsedDocument,
    ParsedPdfDocument,
    ParsedTextDocument,
    ParserLimits,
    ParserMediaType,
    ProcessingJobV1,
)
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


class ParserTransportError(DocumentParserError):
    """Raised when the isolated parser cannot complete its private transport."""


class ParserProtocolError(DocumentParserError):
    """Raised when the isolated parser violates its versioned response contract."""


class ProcessingDispatchError(Exception):
    """Report that a canonical processing job could not be published."""


class ProcessingDispatcher(Protocol):
    """Publish application-owned processing jobs through an interchangeable adapter."""

    def dispatch(self, job: ProcessingJobV1) -> None:
        """Publish one validated job or raise a provider-neutral dispatch error."""


type ParsedDocumentResult = ParsedDocument | ParsedPdfDocument | ParsedTextDocument


@dataclass(frozen=True, slots=True)
class ProcessingOriginal:
    """Verified original stream plus the media type required for safe dispatch."""

    content: BinaryIO
    media_type: ParserMediaType


class DocumentParser(Protocol):
    """Extract ordered source blocks through an interchangeable parser adapter."""

    @property
    def parser_version(self) -> ConfigurationVersion:
        """Return the stable implementation version recorded on processing runs."""

    def parse(
        self,
        content: BinaryIO,
        *,
        limits: ParserLimits,
        media_type: ParserMediaType = "application/pdf",
    ) -> ParsedDocumentResult:
        """Parse one typed binary stream without closing its caller-owned resource."""


class ProcessingOriginalProvider(Protocol):
    """Open one canonical original without exposing document persistence details."""

    def open_for_processing(
        self,
        context: WorkspaceContext,
        document_version_id: UUID,
    ) -> AbstractContextManager[ProcessingOriginal]:
        """Yield a verified typed stream and close it afterward."""


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

    def list_active_chunks(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> Sequence[Chunk]:
        """Return active canonical chunks for one scoped processing run in stable order."""

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


class ProcessingReadUnitOfWork(Protocol):
    """Processing repository scoped to one non-writing status lookup."""

    @property
    def processing(self) -> ProcessingRepository:
        """Return the repository used to resolve a visible processing run."""


type ProcessingUnitOfWorkFactory = Callable[
    [],
    AbstractContextManager[ProcessingUnitOfWork],
]
type ProcessingReadUnitOfWorkFactory = Callable[
    [],
    AbstractContextManager[ProcessingReadUnitOfWork],
]


class ProcessingJobExecutor(Protocol):
    """Execute one validated processing job through a trusted workspace boundary."""

    def execute(self, job: ProcessingJobV1) -> ProcessingRun:
        """Validate canonical scope and process the job's referenced run."""
