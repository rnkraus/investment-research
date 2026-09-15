"""Application services for creating and advancing document processing."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from investment_research.modules.identity.application import IdentityContextProvider
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.contracts import (
    ParsedDocument,
    ParserLimits,
    ProcessingJobV1,
)
from investment_research.modules.processing.domain import (
    Chunk,
    ChunkSet,
    ConfigurationVersion,
    ProcessingAttemptLimitError,
    ProcessingLeaseActiveError,
    ProcessingRun,
    ProcessingState,
    StructureBlock,
    StructureBlockType,
)
from investment_research.modules.processing.ports import (
    DocumentParser,
    DocumentParserError,
    ParserLimitExceededError,
    ProcessingExecutionLimitError,
    ProcessingOriginalProvider,
    ProcessingReadUnitOfWorkFactory,
    ProcessingUnitOfWorkFactory,
)

type Clock = Callable[[], datetime]
type IdentifierFactory = Callable[[], UUID]


@dataclass(frozen=True, slots=True)
class ProcessingProfile:
    """Version identifiers that make one processing attempt reproducible."""

    parser_version: ConfigurationVersion
    parser_config_version: ConfigurationVersion
    chunker_version: ConfigurationVersion
    chunking_config_version: ConfigurationVersion
    lexical_config_version: ConfigurationVersion
    maximum_attempts: int
    lease_duration: timedelta

    def __post_init__(self) -> None:
        """Reject an unsafe execution policy before a service starts."""

        if self.maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive")
        if self.lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")


M0_0_PROCESSING_PROFILE = ProcessingProfile(
    parser_version="pypdf-6.16.2",
    parser_config_version="m0.0-pdf-bounds-v1",
    chunker_version="m0.0-structure-block-v1",
    chunking_config_version="m0.0-structure-block-config-v1",
    lexical_config_version="m0.0-english-lexical-v1",
    maximum_attempts=3,
    lease_duration=timedelta(seconds=360),
)


class ProcessingRunNotActiveError(ValueError):
    """Raised when lexical output is requested for a run outside PARSING."""


class NoExtractableTextError(ValueError):
    """Raised when parsed pages contain no visible text to index."""


class ProcessingRunUnavailableError(Exception):
    """Hide whether a run is unknown or belongs to another workspace."""


class ProcessingRunConflictError(Exception):
    """Raised when the stored run changed during one processing attempt."""


class ProcessingParserVersionError(Exception):
    """Raised when a runner cannot reproduce the run's recorded parser version."""


class ProcessingRunAlreadyActiveError(Exception):
    """Report a harmless duplicate delivery while another lease is active."""


@dataclass(frozen=True, slots=True)
class ProcessingClaim:
    """Outcome of the short canonical claim transaction."""

    run: ProcessingRun
    claimed: bool


@dataclass(frozen=True, slots=True)
class GetProcessingRunService:
    """Return one workspace-visible processing run without disclosing foreign records."""

    unit_of_work_factory: ProcessingReadUnitOfWorkFactory

    def get(self, context: WorkspaceContext, processing_run_id: UUID) -> ProcessingRun:
        """Resolve one run or raise the shared non-disclosing unavailable error."""

        with self.unit_of_work_factory() as unit_of_work:
            run = unit_of_work.processing.get_run_by_id(context, processing_run_id)
        if run is None:
            raise ProcessingRunUnavailableError("processing run is unavailable")
        return run


@dataclass(frozen=True, slots=True)
class LexicalOutput:
    """One internally consistent set of canonical blocks and retrieval chunks."""

    blocks: tuple[StructureBlock, ...]
    chunk_set: ChunkSet
    chunks: tuple[Chunk, ...]


def create_captured_processing_run(
    *,
    context: WorkspaceContext,
    document_version_id: UUID,
    processing_run_id: UUID,
    created_at: datetime,
    profile: ProcessingProfile = M0_0_PROCESSING_PROFILE,
) -> ProcessingRun:
    """Create the durable processing intent associated with a captured original."""

    return ProcessingRun(
        id=processing_run_id,
        workspace_id=context.workspace_id,
        document_version_id=document_version_id,
        state=ProcessingState.CAPTURED,
        parser_version=profile.parser_version,
        parser_config_version=profile.parser_config_version,
        chunker_version=profile.chunker_version,
        chunking_config_version=profile.chunking_config_version,
        lexical_config_version=profile.lexical_config_version,
        created_at=created_at,
    )


def build_lexical_output(
    *,
    run: ProcessingRun,
    parsed_document: ParsedDocument,
    created_at: datetime,
    id_factory: Callable[[], UUID] = uuid4,
) -> LexicalOutput:
    """Map visible PDF pages to the deterministic M0.0 lexical representation."""

    if run.state is not ProcessingState.PARSING:
        raise ProcessingRunNotActiveError("lexical output requires a PARSING run")

    visible_pages = tuple(page for page in parsed_document.pages if page.text.strip())
    if not visible_pages:
        raise NoExtractableTextError("parsed document contains no extractable text")

    chunk_set = ChunkSet(
        id=id_factory(),
        workspace_id=run.workspace_id,
        processing_run_id=run.id,
        document_version_id=run.document_version_id,
        chunker_version=run.chunker_version,
        chunking_config_version=run.chunking_config_version,
        lexical_config_version=run.lexical_config_version,
        is_active=True,
        created_at=created_at,
    )
    blocks: list[StructureBlock] = []
    chunks: list[Chunk] = []
    for ordinal, page in enumerate(visible_pages):
        block = StructureBlock(
            id=id_factory(),
            workspace_id=run.workspace_id,
            processing_run_id=run.id,
            document_version_id=run.document_version_id,
            page_number=page.page_number,
            block_type=StructureBlockType.PARAGRAPH,
            ordinal=ordinal,
            text=page.text,
            start_character=0,
            end_character=len(page.text),
            created_at=created_at,
        )
        blocks.append(block)
        chunks.append(
            Chunk(
                id=id_factory(),
                workspace_id=run.workspace_id,
                processing_run_id=run.id,
                document_version_id=run.document_version_id,
                chunk_set_id=chunk_set.id,
                structure_block_id=block.id,
                ordinal=ordinal,
                text=block.text,
                token_count=len(block.text.split()),
                created_at=created_at,
            )
        )

    return LexicalOutput(
        blocks=tuple(blocks),
        chunk_set=chunk_set,
        chunks=tuple(chunks),
    )


class ProcessDocumentService:
    """Run one captured document through bounded parsing and lexical persistence."""

    def __init__(
        self,
        *,
        parser: DocumentParser,
        original_provider: ProcessingOriginalProvider,
        unit_of_work_factory: ProcessingUnitOfWorkFactory,
        parser_limits: ParserLimits,
        profile: ProcessingProfile = M0_0_PROCESSING_PROFILE,
        clock: Clock = lambda: datetime.now(UTC),
        identifier_factory: IdentifierFactory = uuid4,
        lease_identifier_factory: IdentifierFactory = uuid4,
    ) -> None:
        self._parser = parser
        self._original_provider = original_provider
        self._unit_of_work_factory = unit_of_work_factory
        self._parser_limits = parser_limits
        self._profile = profile
        self._clock = clock
        self._identifier_factory = identifier_factory
        self._lease_identifier_factory = lease_identifier_factory

    def process(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> ProcessingRun:
        """Process one run by identifier and return its committed terminal state."""

        claim = self._claim_run(context, processing_run_id)
        if not claim.claimed:
            return claim.run
        parsing_run = claim.run
        lease_id = parsing_run.lease_id
        if lease_id is None:
            raise RuntimeError("claimed processing run has no lease identifier")
        try:
            self._require_parser_version(parsing_run)
            with self._original_provider.open_for_processing(
                context,
                parsing_run.document_version_id,
            ) as original:
                parsed_document = self._parser.parse(original, limits=self._parser_limits)

            completed_at = self._aware_utc_now()
            output = build_lexical_output(
                run=parsing_run,
                parsed_document=parsed_document,
                created_at=completed_at,
                id_factory=self._identifier_factory,
            )
            ready_run = parsing_run.mark_lexical_ready(
                lease_id=lease_id,
                completed_at=completed_at,
            )
            self._commit_output(context, parsing_run, ready_run, output)
        except Exception as error:
            try:
                self._mark_failed(context, parsing_run, error)
            except Exception as recording_error:
                error.add_note(
                    "processing failure state could not be recorded: "
                    f"{type(recording_error).__name__}"
                )
            raise

        return ready_run

    def _claim_run(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> ProcessingClaim:
        """Atomically claim eligible work or return a terminal no-op."""

        with self._unit_of_work_factory() as unit_of_work:
            current_run = unit_of_work.processing.get_run_by_id_for_update(
                context,
                processing_run_id,
            )
            if current_run is None:
                raise ProcessingRunUnavailableError("processing run is unavailable")

            if current_run.state in {
                ProcessingState.LEXICAL_READY,
                ProcessingState.FAILED,
            }:
                return ProcessingClaim(run=current_run, claimed=False)

            claimed_at = self._aware_utc_now()
            try:
                parsing_run = current_run.claim_for_processing(
                    lease_id=self._lease_identifier_factory(),
                    claimed_at=claimed_at,
                    lease_expires_at=claimed_at + self._profile.lease_duration,
                    maximum_attempts=self._profile.maximum_attempts,
                )
            except ProcessingLeaseActiveError as error:
                raise ProcessingRunAlreadyActiveError(
                    "processing run already has an active attempt"
                ) from error
            except ProcessingAttemptLimitError as error:
                exhausted_run = current_run.mark_attempts_exhausted(
                    completed_at=claimed_at,
                    error_summary="Processing attempt limit exceeded",
                )
                if not unit_of_work.processing.update_run(context, exhausted_run):
                    raise ProcessingRunUnavailableError("processing run is unavailable") from error
                return ProcessingClaim(run=exhausted_run, claimed=False)

            if not unit_of_work.processing.update_run(context, parsing_run):
                raise ProcessingRunUnavailableError("processing run is unavailable")
        return ProcessingClaim(run=parsing_run, claimed=True)

    def _commit_output(
        self,
        context: WorkspaceContext,
        parsing_run: ProcessingRun,
        ready_run: ProcessingRun,
        output: LexicalOutput,
    ) -> None:
        """Commit derived output and the ready state in one transaction."""

        with self._unit_of_work_factory() as unit_of_work:
            current_run = unit_of_work.processing.get_run_by_id_for_update(
                context,
                parsing_run.id,
            )
            self._require_unchanged_active_run(current_run, parsing_run)
            unit_of_work.processing.add_lexical_output(
                context,
                output.blocks,
                output.chunk_set,
                output.chunks,
            )
            if not unit_of_work.processing.update_run(context, ready_run):
                raise ProcessingRunUnavailableError("processing run is unavailable")

    def _mark_failed(
        self,
        context: WorkspaceContext,
        parsing_run: ProcessingRun,
        error: Exception,
    ) -> None:
        """Record a sanitized terminal failure in a fresh transaction."""

        lease_id = parsing_run.lease_id
        if lease_id is None:
            raise RuntimeError("active processing run has no lease identifier")
        with self._unit_of_work_factory() as unit_of_work:
            current_run = unit_of_work.processing.get_run_by_id_for_update(
                context,
                parsing_run.id,
            )
            self._require_unchanged_active_run(current_run, parsing_run)
            failed_run = parsing_run.mark_failed(
                lease_id=lease_id,
                completed_at=self._aware_utc_now(),
                error_summary=self._safe_error_summary(error),
            )
            if not unit_of_work.processing.update_run(context, failed_run):
                raise ProcessingRunUnavailableError("processing run is unavailable")

    def _require_parser_version(self, run: ProcessingRun) -> None:
        """Refuse processing when the installed parser differs from the run contract."""

        if self._parser.parser_version != run.parser_version:
            raise ProcessingParserVersionError(
                "runner parser version does not match the processing run"
            )

    @staticmethod
    def _require_unchanged_active_run(
        current_run: ProcessingRun | None,
        expected_run: ProcessingRun,
    ) -> None:
        """Ensure a locked run is still the exact active snapshot this worker started."""

        if current_run is None:
            raise ProcessingRunUnavailableError("processing run is unavailable")
        if current_run != expected_run:
            raise ProcessingRunConflictError("processing run changed during execution")

    @staticmethod
    def _safe_error_summary(error: Exception) -> str:
        """Map failures to bounded messages without persisting untrusted exception text."""

        if isinstance(error, ParserLimitExceededError):
            return "Parser safety limit exceeded"
        if isinstance(error, ProcessingExecutionLimitError):
            return "Processing execution time limit exceeded"
        if isinstance(error, MemoryError):
            return "Processing memory limit exceeded"
        if isinstance(error, DocumentParserError):
            return "Document parsing failed safely"
        if isinstance(error, NoExtractableTextError):
            return "Document contains no extractable text"
        if isinstance(error, ProcessingParserVersionError):
            return "Configured parser version is unavailable"
        return "Processing failed safely"

    def _aware_utc_now(self) -> datetime:
        """Require an aware clock and normalize its instant before any write."""

        current_time = self._clock()
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise RuntimeError("processing clock must return a timezone-aware datetime")
        return current_time.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ExecuteProcessingJobService:
    """Execute a queue job only within server-controlled workspace scope."""

    processing_service: ProcessDocumentService
    identity_context_provider: IdentityContextProvider

    def execute(self, job: ProcessingJobV1) -> ProcessingRun:
        """Resolve trusted scope and process one matching canonical run."""

        context = self.identity_context_provider.resolve()
        if job.workspace_id != context.workspace_id:
            raise ProcessingRunUnavailableError("processing run is unavailable")
        return self.processing_service.process(context, job.processing_run_id)
