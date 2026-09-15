"""Application services for secure immutable document originals."""

import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from logging import getLogger
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Final, cast
from uuid import UUID, uuid4

from investment_research.modules.documents.contracts import (
    CaptureDocumentInput,
    CaptureDocumentResult,
)
from investment_research.modules.documents.domain import (
    Document,
    DocumentLifecycleState,
    DocumentVersion,
    OriginalArtifact,
    OriginalMediaType,
)
from investment_research.modules.documents.ports import (
    BinaryReadable,
    CaptureUnitOfWorkFactory,
    DocumentReadUnitOfWorkFactory,
    ObjectNotFoundError,
    ObjectStorage,
    ObjectStorageError,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import (
    M0_0_PROCESSING_PROFILE,
    ProcessingProfile,
    create_captured_processing_run,
)
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.ports import (
    ProcessingDispatcher,
    ProcessingDispatchError,
)

PDF_HEADER_PATTERN: Final = re.compile(rb"%PDF-(?:1\.[0-9]|2\.0)")
PDF_HEADER_LENGTH: Final = 8
READ_CHUNK_SIZE: Final = 1024 * 1024
M0_0_DOCUMENT_TYPE: Final = "earnings_brief"
LOGGER = getLogger("investment_research.documents.capture")

type Clock = Callable[[], datetime]
type IdentifierFactory = Callable[[], UUID]


class DocumentCaptureError(Exception):
    """Base error safe for translation at the API boundary."""


class UnsupportedDocumentTypeError(DocumentCaptureError):
    """Raised when upload metadata does not describe an allowed PDF."""


class InvalidPdfSignatureError(DocumentCaptureError):
    """Raised when content does not begin with a supported PDF signature."""


class UploadTooLargeError(DocumentCaptureError):
    """Raised as soon as an upload exceeds its configured byte limit."""


class OriginalUnavailableError(Exception):
    """Hide whether document identifiers are unknown, foreign, or ineligible."""


class OriginalIntegrityError(Exception):
    """Raised when stored bytes no longer match canonical original metadata."""


@dataclass(slots=True)
class OriginalDownload:
    """Verified original plus safe response metadata and owned stream lifecycle."""

    media_type: OriginalMediaType
    byte_size: int
    filename: str
    _content: SpooledTemporaryFile[bytes]

    def iter_bytes(self) -> Generator[bytes]:
        """Yield bounded chunks and always close the prepared content afterward."""

        try:
            while chunk := self._content.read(READ_CHUNK_SIZE):
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        """Release memory or the private temporary file idempotently."""

        self._content.close()


class OpenOriginalService:
    """Resolve and verify one eligible immutable original before download."""

    def __init__(
        self,
        *,
        object_storage: ObjectStorage,
        unit_of_work_factory: DocumentReadUnitOfWorkFactory,
    ) -> None:
        self._object_storage = object_storage
        self._unit_of_work_factory = unit_of_work_factory

    def open_original(
        self,
        context: WorkspaceContext,
        document_id: UUID,
        version_id: UUID,
    ) -> OriginalDownload:
        """Return verified content only for the active in-scope document version."""

        with self._unit_of_work_factory() as unit_of_work:
            original = unit_of_work.documents.get_original_artifact(
                context,
                document_id,
                version_id,
            )
        if original is None:
            raise OriginalUnavailableError("original is unavailable")

        return self._prepare_download(
            context,
            original,
            filename=f"document-{document_id}-version-{version_id}.pdf",
        )

    @contextmanager
    def open_for_processing(
        self,
        context: WorkspaceContext,
        document_version_id: UUID,
    ) -> Generator[BinaryIO]:
        """Yield one verified current original for an internal processing run."""

        with self._unit_of_work_factory() as unit_of_work:
            original = unit_of_work.documents.get_original_artifact_by_version_id(
                context,
                document_version_id,
            )
        if original is None:
            raise OriginalUnavailableError("original is unavailable")

        download = self._prepare_download(
            context,
            original,
            filename=f"document-version-{document_version_id}.pdf",
        )
        try:
            yield cast(BinaryIO, download._content)
        finally:
            download.close()

    def _prepare_download(
        self,
        context: WorkspaceContext,
        original: OriginalArtifact,
        *,
        filename: str,
    ) -> OriginalDownload:
        """Copy and verify one canonical original into a private bounded spool."""

        prepared = SpooledTemporaryFile[bytes](
            max_size=min(original.byte_size, READ_CHUNK_SIZE),
            mode="w+b",
        )
        try:
            digest = sha256()
            byte_size = 0
            with self._object_storage.open(context, original.storage_key) as stored_file:
                while chunk := stored_file.read(READ_CHUNK_SIZE):
                    if not isinstance(chunk, bytes):
                        raise OriginalIntegrityError("stored original is not a binary stream")
                    byte_size += len(chunk)
                    if byte_size > original.byte_size:
                        raise OriginalIntegrityError("stored original exceeds its recorded size")
                    digest.update(chunk)
                    prepared.write(chunk)
        except ObjectNotFoundError as error:
            prepared.close()
            raise OriginalIntegrityError("stored original is unavailable") from error
        except BaseException:
            prepared.close()
            raise

        if byte_size != original.byte_size or digest.hexdigest() != original.checksum_sha256:
            prepared.close()
            raise OriginalIntegrityError("stored original does not match canonical metadata")

        prepared.seek(0)
        return OriginalDownload(
            media_type=original.media_type,
            byte_size=original.byte_size,
            filename=filename,
            _content=prepared,
        )


class CaptureDocumentService:
    """Validate and atomically record one immutable document capture."""

    def __init__(
        self,
        *,
        object_storage: ObjectStorage,
        unit_of_work_factory: CaptureUnitOfWorkFactory,
        max_upload_bytes: int,
        processing_dispatcher: ProcessingDispatcher,
        processing_profile: ProcessingProfile = M0_0_PROCESSING_PROFILE,
        clock: Clock = lambda: datetime.now(UTC),
        identifier_factory: IdentifierFactory = uuid4,
    ) -> None:
        if max_upload_bytes < PDF_HEADER_LENGTH:
            raise ValueError(f"max_upload_bytes must be at least {PDF_HEADER_LENGTH} bytes")
        self._object_storage = object_storage
        self._unit_of_work_factory = unit_of_work_factory
        self._max_upload_bytes = max_upload_bytes
        self._processing_dispatcher = processing_dispatcher
        self._processing_profile = processing_profile
        self._clock = clock
        self._identifier_factory = identifier_factory

    def capture(
        self,
        context: WorkspaceContext,
        capture_input: CaptureDocumentInput,
        content: BinaryReadable,
    ) -> CaptureDocumentResult:
        """Validate, preserve, and record one PDF plus its processing intent."""

        self._validate_declared_type(capture_input)
        captured_at = self._aware_utc_now()
        document_id = self._identifier_factory()
        document_version_id = self._identifier_factory()
        original_artifact_id = self._identifier_factory()
        processing_run_id = self._identifier_factory()
        storage_key = f"workspaces/{context.workspace_id}/originals/{original_artifact_id}.pdf"

        with SpooledTemporaryFile[bytes](
            max_size=min(self._max_upload_bytes, READ_CHUNK_SIZE),
            mode="w+b",
        ) as validated_content:
            byte_size, checksum = self._copy_validated_pdf(content, validated_content)
            validated_content.seek(0)
            stored_object = self._object_storage.store_immutable(
                context,
                storage_key,
                validated_content,
            )

        try:
            if (
                stored_object.storage_key != storage_key
                or stored_object.byte_size != byte_size
                or stored_object.checksum_sha256 != checksum
            ):
                raise ObjectStorageError("stored object verification failed")
            result = self._record_capture(
                context=context,
                capture_input=capture_input,
                captured_at=captured_at,
                document_id=document_id,
                document_version_id=document_version_id,
                original_artifact_id=original_artifact_id,
                processing_run_id=processing_run_id,
                storage_key=storage_key,
                byte_size=byte_size,
                checksum=checksum,
            )
        except BaseException as capture_error:
            try:
                self._object_storage.discard_uncommitted(context, storage_key)
            except BaseException as cleanup_error:
                capture_error.add_note(
                    f"uncommitted object cleanup failed: {type(cleanup_error).__name__}"
                )
            raise

        self._dispatch_processing(context, result.processing_run_id)
        return result

    def _dispatch_processing(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> None:
        """Publish after commit while preserving canonical state on broker failure."""

        job = ProcessingJobV1.create(
            workspace_id=context.workspace_id,
            processing_run_id=processing_run_id,
        )
        try:
            self._processing_dispatcher.dispatch(job)
        except ProcessingDispatchError:
            LOGGER.warning(
                "processing.dispatch.failed",
                extra={
                    "workspace_id": context.workspace_id,
                    "job_id": processing_run_id,
                },
            )

    def _record_capture(
        self,
        *,
        context: WorkspaceContext,
        capture_input: CaptureDocumentInput,
        captured_at: datetime,
        document_id: UUID,
        document_version_id: UUID,
        original_artifact_id: UUID,
        processing_run_id: UUID,
        storage_key: str,
        byte_size: int,
        checksum: str,
    ) -> CaptureDocumentResult:
        """Build and atomically commit canonical metadata for a stored object."""

        document = Document(
            id=document_id,
            workspace_id=context.workspace_id,
            document_type=M0_0_DOCUMENT_TYPE,
            lifecycle_state=DocumentLifecycleState.ACTIVE,
            current_version_id=document_version_id,
            created_at=captured_at,
        )
        version = DocumentVersion(
            id=document_version_id,
            workspace_id=context.workspace_id,
            document_id=document_id,
            version_number=1,
            title=capture_input.title,
            language=capture_input.language,
            content_sha256=checksum,
            published_at=capture_input.published_at,
            available_at=capture_input.available_at,
            captured_at=captured_at,
        )
        original = OriginalArtifact(
            id=original_artifact_id,
            workspace_id=context.workspace_id,
            document_version_id=document_version_id,
            storage_key=storage_key,
            media_type=OriginalMediaType.PDF,
            byte_size=byte_size,
            checksum_sha256=checksum,
            captured_at=captured_at,
        )
        processing_run = create_captured_processing_run(
            context=context,
            document_version_id=document_version_id,
            processing_run_id=processing_run_id,
            created_at=captured_at,
            profile=self._processing_profile,
        )

        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.documents.add_capture(context, document, version, original)
            unit_of_work.processing.add_run(context, processing_run)

        return CaptureDocumentResult(
            document_id=document_id,
            document_version_id=document_version_id,
            original_artifact_id=original_artifact_id,
            processing_run_id=processing_run_id,
            state=processing_run.state,
        )

    @staticmethod
    def _validate_declared_type(capture_input: CaptureDocumentInput) -> None:
        """Require both an allowed filename extension and declared media type."""

        if not capture_input.filename.casefold().endswith(".pdf"):
            raise UnsupportedDocumentTypeError("only .pdf files are supported")
        if capture_input.media_type.casefold() != OriginalMediaType.PDF.value:
            raise UnsupportedDocumentTypeError("only application/pdf is supported")

    def _copy_validated_pdf(
        self,
        source: BinaryReadable,
        destination: SpooledTemporaryFile[bytes],
    ) -> tuple[int, str]:
        """Copy a bounded PDF to a private spool while calculating its checksum."""

        header = self._read_header(source)
        if PDF_HEADER_PATTERN.fullmatch(header) is None:
            raise InvalidPdfSignatureError("content does not have a supported PDF signature")

        digest = sha256()
        digest.update(header)
        destination.write(header)
        byte_size = len(header)

        while chunk := source.read(READ_CHUNK_SIZE):
            if not isinstance(chunk, bytes):
                raise TypeError("document content must be opened in binary mode")
            byte_size += len(chunk)
            if byte_size > self._max_upload_bytes:
                raise UploadTooLargeError(
                    f"document exceeds the {self._max_upload_bytes}-byte upload limit"
                )
            digest.update(chunk)
            destination.write(chunk)

        return byte_size, digest.hexdigest()

    @staticmethod
    def _read_header(source: BinaryReadable) -> bytes:
        """Read a complete signature even when the source returns short chunks."""

        header = bytearray()
        while len(header) < PDF_HEADER_LENGTH:
            chunk = source.read(PDF_HEADER_LENGTH - len(header))
            if not isinstance(chunk, bytes):
                raise TypeError("document content must be opened in binary mode")
            if not chunk:
                break
            header.extend(chunk)
        return bytes(header)

    def _aware_utc_now(self) -> datetime:
        """Require an aware clock and normalize its instant before any write."""

        current_time = self._clock()
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise RuntimeError("capture clock must return a timezone-aware datetime")
        return current_time.astimezone(UTC)
