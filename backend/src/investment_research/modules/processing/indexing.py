"""Canonical processing lifecycle orchestration for derived dense indexing."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import (
    M1_PROCESSING_PROFILE,
    Clock,
    IdentifierFactory,
    ProcessingProfile,
    ProcessingRetryableError,
    ProcessingRunAlreadyActiveError,
    ProcessingRunConflictError,
    ProcessingRunUnavailableError,
)
from investment_research.modules.processing.domain import (
    Chunk,
    ProcessingAttemptLimitError,
    ProcessingLeaseActiveError,
    ProcessingRun,
    ProcessingState,
)
from investment_research.modules.processing.ports import ProcessingUnitOfWorkFactory
from investment_research.modules.search.indexing import IndexActiveChunksService
from investment_research.modules.search.ports import SearchProviderTransientError


@dataclass(frozen=True, slots=True)
class IndexingClaim:
    """Canonical run and chunks owned by one indexing execution attempt."""

    run: ProcessingRun
    chunks: tuple[Chunk, ...]
    claimed: bool


class IndexProcessingRunService:
    """Advance one canonical M1 run around provider work outside transactions."""

    def __init__(
        self,
        *,
        indexing_service: IndexActiveChunksService,
        unit_of_work_factory: ProcessingUnitOfWorkFactory,
        profile: ProcessingProfile = M1_PROCESSING_PROFILE,
        clock: Clock = lambda: datetime.now(UTC),
        lease_identifier_factory: IdentifierFactory = uuid4,
    ) -> None:
        self._indexing_service = indexing_service
        self._unit_of_work_factory = unit_of_work_factory
        self._profile = profile
        self._clock = clock
        self._lease_identifier_factory = lease_identifier_factory

    def index(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
        *,
        owned_lease_id: UUID | None = None,
    ) -> ProcessingRun:
        """Claim, index, and complete one run or return an existing terminal state."""

        claim = self._claim_for_indexing(
            context,
            processing_run_id,
            owned_lease_id=owned_lease_id,
        )
        if not claim.claimed:
            return claim.run

        indexing_run = claim.run
        try:
            self._indexing_service.index(context, indexing_run, claim.chunks)
        except SearchProviderTransientError as error:
            if indexing_run.attempt_count >= self._profile.maximum_attempts:
                try:
                    self._mark_failed(
                        context,
                        indexing_run,
                        error_summary="Processing attempt limit exceeded",
                    )
                except Exception as recording_error:
                    error.add_note(
                        "indexing failure state could not be recorded: "
                        f"{type(recording_error).__name__}"
                    )
                raise
            try:
                self._expire_lease_for_retry(context, indexing_run)
            except Exception as recording_error:
                error.add_note(
                    f"indexing retry state could not be recorded: {type(recording_error).__name__}"
                )
                raise
            raise ProcessingRetryableError(
                "indexing dependency is temporarily unavailable"
            ) from None
        except Exception as error:
            try:
                self._mark_failed(
                    context,
                    indexing_run,
                    error_summary="Dense indexing failed safely",
                )
            except Exception as recording_error:
                error.add_note(
                    "indexing failure state could not be recorded: "
                    f"{type(recording_error).__name__}"
                )
            raise

        return self._mark_searchable(context, indexing_run)

    def _claim_for_indexing(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
        *,
        owned_lease_id: UUID | None,
    ) -> IndexingClaim:
        """Claim resumable indexing and load canonical chunks in one short transaction."""

        with self._unit_of_work_factory() as unit_of_work:
            current_run = unit_of_work.processing.get_run_by_id_for_update(
                context,
                processing_run_id,
            )
            if current_run is None:
                raise ProcessingRunUnavailableError("processing run is unavailable")
            if current_run.state in {ProcessingState.SEARCHABLE, ProcessingState.FAILED}:
                return IndexingClaim(run=current_run, chunks=(), claimed=False)
            if current_run.state not in {
                ProcessingState.LEXICAL_READY,
                ProcessingState.INDEXING,
            }:
                raise ProcessingRunConflictError("processing run is not ready for dense indexing")

            claimed_at = self._aware_utc_now()
            lease_expires_at = current_run.lease_expires_at
            if lease_expires_at is not None and lease_expires_at > claimed_at:
                if owned_lease_id is None or current_run.lease_id != owned_lease_id:
                    raise ProcessingRunAlreadyActiveError(lease_expires_at)
                claimed_run = current_run
            else:
                try:
                    claimed_run = current_run.claim_for_indexing(
                        lease_id=self._lease_identifier_factory(),
                        claimed_at=claimed_at,
                        lease_expires_at=claimed_at + self._profile.lease_duration,
                        maximum_attempts=self._profile.maximum_attempts,
                    )
                except ProcessingLeaseActiveError as error:
                    lease_expires_at = current_run.lease_expires_at
                    if lease_expires_at is None:
                        raise RuntimeError("active indexing run has no lease expiry") from error
                    raise ProcessingRunAlreadyActiveError(lease_expires_at) from error
                except ProcessingAttemptLimitError as error:
                    exhausted_run = current_run.mark_attempts_exhausted(
                        completed_at=claimed_at,
                        error_summary="Processing attempt limit exceeded",
                    )
                    if not unit_of_work.processing.update_run(context, exhausted_run):
                        raise ProcessingRunUnavailableError(
                            "processing run is unavailable"
                        ) from error
                    return IndexingClaim(run=exhausted_run, chunks=(), claimed=False)

            indexing_run = (
                claimed_run.begin_indexing(lease_id=claimed_run.lease_id)
                if claimed_run.state is ProcessingState.LEXICAL_READY
                and claimed_run.lease_id is not None
                else claimed_run
            )
            if indexing_run != current_run and not unit_of_work.processing.update_run(
                context,
                indexing_run,
            ):
                raise ProcessingRunUnavailableError("processing run is unavailable")
            chunks = tuple(unit_of_work.processing.list_active_chunks(context, processing_run_id))
        return IndexingClaim(run=indexing_run, chunks=chunks, claimed=True)

    def _mark_searchable(
        self,
        context: WorkspaceContext,
        indexing_run: ProcessingRun,
    ) -> ProcessingRun:
        """Commit SEARCHABLE only after every deterministic vector is visible."""

        lease_id = self._require_lease_id(indexing_run)
        with self._unit_of_work_factory() as unit_of_work:
            current_run = unit_of_work.processing.get_run_by_id_for_update(
                context,
                indexing_run.id,
            )
            self._require_unchanged_run(current_run, indexing_run)
            searchable_run = indexing_run.mark_searchable(
                lease_id=lease_id,
                completed_at=self._aware_utc_now(),
            )
            if not unit_of_work.processing.update_run(context, searchable_run):
                raise ProcessingRunUnavailableError("processing run is unavailable")
        return searchable_run

    def _expire_lease_for_retry(
        self,
        context: WorkspaceContext,
        indexing_run: ProcessingRun,
    ) -> None:
        """Make a transiently failed indexing run immediately reclaimable."""

        lease_id = self._require_lease_id(indexing_run)
        with self._unit_of_work_factory() as unit_of_work:
            current_run = unit_of_work.processing.get_run_by_id_for_update(
                context,
                indexing_run.id,
            )
            self._require_unchanged_run(current_run, indexing_run)
            retryable_run = indexing_run.expire_lease_for_retry(
                lease_id=lease_id,
                expired_at=self._aware_utc_now(),
            )
            if not unit_of_work.processing.update_run(context, retryable_run):
                raise ProcessingRunUnavailableError("processing run is unavailable")

    def _mark_failed(
        self,
        context: WorkspaceContext,
        indexing_run: ProcessingRun,
        *,
        error_summary: str,
    ) -> None:
        """Record one bounded terminal indexing failure in a fresh transaction."""

        lease_id = self._require_lease_id(indexing_run)
        with self._unit_of_work_factory() as unit_of_work:
            current_run = unit_of_work.processing.get_run_by_id_for_update(
                context,
                indexing_run.id,
            )
            self._require_unchanged_run(current_run, indexing_run)
            failed_run = indexing_run.mark_failed(
                lease_id=lease_id,
                completed_at=self._aware_utc_now(),
                error_summary=error_summary,
            )
            if not unit_of_work.processing.update_run(context, failed_run):
                raise ProcessingRunUnavailableError("processing run is unavailable")

    @staticmethod
    def _require_unchanged_run(
        current_run: ProcessingRun | None,
        expected_run: ProcessingRun,
    ) -> None:
        """Require the locked canonical state to match the provider-work snapshot."""

        if current_run is None:
            raise ProcessingRunUnavailableError("processing run is unavailable")
        if current_run != expected_run:
            raise ProcessingRunConflictError("processing run changed during indexing")

    @staticmethod
    def _require_lease_id(run: ProcessingRun) -> UUID:
        """Return the owned lease required for a canonical indexing transition."""

        if run.lease_id is None:
            raise RuntimeError("active indexing run has no lease identifier")
        return run.lease_id

    def _aware_utc_now(self) -> datetime:
        """Require an aware clock and normalize its instant before any write."""

        current_time = self._clock()
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise RuntimeError("processing clock must return a timezone-aware datetime")
        return current_time.astimezone(UTC)
