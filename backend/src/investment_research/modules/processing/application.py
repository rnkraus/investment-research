"""Application services for creating and advancing document processing."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.identity.application import IdentityContextProvider
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.contracts import (
    M1_PARSER_CONFIG_VERSION,
    PDF_PARSER_CONFIG_VERSION_V1,
    ParsedDocument,
    ParserLimits,
    ProcessingJobV1,
    ProcessingStatusQuery,
)
from investment_research.modules.processing.domain import (
    M0_DISABLED_DENSE_INDEX_CONFIG_VERSION,
    M0_DISABLED_EMBEDDING_CONFIG_VERSION,
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
    ParsedDocumentResult,
    ParserLimitExceededError,
    ParserTransportError,
    ProcessingOriginalProvider,
    ProcessingReadUnitOfWorkFactory,
    ProcessingRunIndexer,
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
    embedding_config_version: ConfigurationVersion
    dense_index_config_version: ConfigurationVersion
    maximum_attempts: int
    lease_duration: timedelta

    def __post_init__(self) -> None:
        """Reject an unsafe execution policy before a service starts."""

        if self.maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive")
        if self.lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")


M0_0_PROCESSING_PROFILE = ProcessingProfile(
    parser_version="pypdf-6.21.2",
    parser_config_version=PDF_PARSER_CONFIG_VERSION_V1,
    chunker_version="m0.0-structure-block-v1",
    chunking_config_version="m0.0-structure-block-config-v1",
    lexical_config_version="m0.0-english-lexical-v1",
    embedding_config_version=M0_DISABLED_EMBEDDING_CONFIG_VERSION,
    dense_index_config_version=M0_DISABLED_DENSE_INDEX_CONFIG_VERSION,
    maximum_attempts=3,
    lease_duration=timedelta(seconds=360),
)

M1_PROCESSING_PROFILE = ProcessingProfile(
    parser_version="pypdf-6.21.2",
    parser_config_version=M1_PARSER_CONFIG_VERSION,
    chunker_version="m1-source-block-v1",
    chunking_config_version="m1-source-block-config-v1",
    lexical_config_version="m1-english-lexical-v1",
    embedding_config_version="openai-text-embedding-3-small-1536-v1",
    dense_index_config_version="pinecone-serverless-cosine-1536-aws-us-east-1-v1",
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
    """Raised when processing cannot reproduce the run's recorded parser version."""


class ProcessingRunAlreadyActiveError(Exception):
    """Report when another attempt owns the run until a known UTC time."""

    def __init__(self, retry_at: datetime) -> None:
        super().__init__("processing run already has an active attempt")
        self.retry_at = retry_at


class ProcessingRetryableError(Exception):
    """Request one bounded retry after canonical state is made reclaimable."""


@dataclass(frozen=True, slots=True)
class ProcessingClaim:
    """Outcome of the short canonical claim transaction."""

    run: ProcessingRun
    claimed: bool


@dataclass(frozen=True, slots=True)
class ProcessingAttemptResult:
    """Committed run plus any indexing lease owned by this exact invocation."""

    run: ProcessingRun
    owned_indexing_lease_id: UUID | None


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

    def get_latest_for_versions(
        self,
        context: WorkspaceContext,
        query: ProcessingStatusQuery,
    ) -> tuple[ProcessingRun, ...]:
        """Resolve one bounded status batch without keeping its read session open."""

        if not query.document_version_ids:
            return ()
        with self.unit_of_work_factory() as unit_of_work:
            return unit_of_work.processing.list_latest_runs_for_versions(context, query)


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
        embedding_config_version=profile.embedding_config_version,
        dense_index_config_version=profile.dense_index_config_version,
        created_at=created_at,
    )


def build_lexical_output(
    *,
    run: ProcessingRun,
    parsed_document: ParsedDocumentResult,
    created_at: datetime,
    id_factory: Callable[[], UUID] = uuid4,
) -> LexicalOutput:
    """Map v1 pages or exact v2 blocks to one deterministic lexical representation."""

    if run.state is not ProcessingState.PARSING:
        raise ProcessingRunNotActiveError("lexical output requires a PARSING run")

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
    source_blocks: tuple[
        tuple[
            SourceLocatorKind,
            int | None,
            StructureBlockType,
            int,
            str,
            int,
            int,
        ],
        ...,
    ]
    if isinstance(parsed_document, ParsedDocument):
        visible_pages = tuple(page for page in parsed_document.pages if page.text.strip())
        source_blocks = tuple(
            (
                SourceLocatorKind.PDF_PAGE,
                page.page_number,
                StructureBlockType.PARAGRAPH,
                ordinal,
                page.text,
                0,
                len(page.text),
            )
            for ordinal, page in enumerate(visible_pages)
        )
    else:
        source_blocks = tuple(
            (
                block.source_locator_kind,
                block.page_number,
                block.block_type,
                block.ordinal,
                block.text,
                block.start_character,
                block.end_character,
            )
            for block in parsed_document.blocks
        )
    if not source_blocks:
        raise NoExtractableTextError("parsed document contains no extractable text")

    for (
        source_locator_kind,
        page_number,
        block_type,
        ordinal,
        text,
        start_character,
        end_character,
    ) in source_blocks:
        block = StructureBlock(
            id=id_factory(),
            workspace_id=run.workspace_id,
            processing_run_id=run.id,
            document_version_id=run.document_version_id,
            source_locator_kind=source_locator_kind,
            page_number=page_number,
            block_type=block_type,
            ordinal=ordinal,
            text=text,
            start_character=start_character,
            end_character=end_character,
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
        """Parse one run by identifier and return its committed next-stage state."""

        return self.process_attempt(context, processing_run_id).run

    def process_attempt(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> ProcessingAttemptResult:
        """Parse one run and report only indexing lease ownership acquired here."""

        claim = self._claim_run(context, processing_run_id)
        if not claim.claimed:
            return ProcessingAttemptResult(
                run=claim.run,
                owned_indexing_lease_id=None,
            )
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
                parsed_document = self._parser.parse(
                    original.content,
                    limits=self._parser_limits,
                    media_type=original.media_type,
                )

            completed_at = self._aware_utc_now()
            output = build_lexical_output(
                run=parsing_run,
                parsed_document=parsed_document,
                created_at=completed_at,
                id_factory=self._identifier_factory,
            )
            ready_run = (
                parsing_run.mark_lexical_ready_for_indexing(lease_id=lease_id)
                if parsing_run.dense_indexing_enabled
                else parsing_run.mark_lexical_ready(
                    lease_id=lease_id,
                    completed_at=completed_at,
                )
            )
            self._commit_output(context, parsing_run, ready_run, output)
        except ParserTransportError as error:
            if parsing_run.attempt_count >= self._profile.maximum_attempts:
                try:
                    self._mark_failed(
                        context,
                        parsing_run,
                        error,
                        error_summary="Processing attempt limit exceeded",
                    )
                except Exception as recording_error:
                    error.add_note(
                        "processing failure state could not be recorded: "
                        f"{type(recording_error).__name__}"
                    )
                raise
            try:
                self._expire_lease_for_retry(context, parsing_run)
            except Exception as recording_error:
                error.add_note(
                    "processing retry state could not be recorded: "
                    f"{type(recording_error).__name__}"
                )
                raise
            raise ProcessingRetryableError(
                "processing dependency is temporarily unavailable"
            ) from None
        except Exception as error:
            try:
                self._mark_failed(context, parsing_run, error)
            except Exception as recording_error:
                error.add_note(
                    "processing failure state could not be recorded: "
                    f"{type(recording_error).__name__}"
                )
            raise

        return ProcessingAttemptResult(
            run=ready_run,
            owned_indexing_lease_id=lease_id if ready_run.dense_indexing_enabled else None,
        )

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
                ProcessingState.INDEXING,
                ProcessingState.SEARCHABLE,
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
                lease_expires_at = current_run.lease_expires_at
                if lease_expires_at is None:
                    raise RuntimeError("active processing run has no lease expiry") from error
                raise ProcessingRunAlreadyActiveError(lease_expires_at) from error
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
        *,
        error_summary: str | None = None,
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
                error_summary=error_summary or self._safe_error_summary(error),
            )
            if not unit_of_work.processing.update_run(context, failed_run):
                raise ProcessingRunUnavailableError("processing run is unavailable")

    def _expire_lease_for_retry(
        self,
        context: WorkspaceContext,
        parsing_run: ProcessingRun,
    ) -> None:
        """Commit an immediately reclaimable lease after a transient failure."""

        lease_id = parsing_run.lease_id
        if lease_id is None:
            raise RuntimeError("active processing run has no lease identifier")
        with self._unit_of_work_factory() as unit_of_work:
            current_run = unit_of_work.processing.get_run_by_id_for_update(
                context,
                parsing_run.id,
            )
            self._require_unchanged_active_run(current_run, parsing_run)
            retryable_run = parsing_run.expire_lease_for_retry(
                lease_id=lease_id,
                expired_at=self._aware_utc_now(),
            )
            if not unit_of_work.processing.update_run(context, retryable_run):
                raise ProcessingRunUnavailableError("processing run is unavailable")

    def _require_parser_version(self, run: ProcessingRun) -> None:
        """Refuse processing when the installed parser differs from the run contract."""

        if self._parser.parser_version != run.parser_version:
            raise ProcessingParserVersionError(
                "configured parser version does not match the processing run"
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
    indexing_service: ProcessingRunIndexer | None
    identity_context_provider: IdentityContextProvider

    def execute(self, job: ProcessingJobV1) -> ProcessingRun:
        """Resolve trusted scope and process one matching canonical run."""

        context = self.identity_context_provider.resolve()
        if job.workspace_id != context.workspace_id:
            raise ProcessingRunUnavailableError("processing run is unavailable")
        processing_attempt = self.processing_service.process_attempt(
            context,
            job.processing_run_id,
        )
        processed_run = processing_attempt.run
        if not processed_run.dense_indexing_enabled or processed_run.state not in {
            ProcessingState.LEXICAL_READY,
            ProcessingState.INDEXING,
        }:
            return processed_run
        if self.indexing_service is None:
            raise RuntimeError("dense indexing service is required for this processing profile")
        return self.indexing_service.index(
            context,
            processed_run.id,
            owned_lease_id=processing_attempt.owned_indexing_lease_id,
        )
