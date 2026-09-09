"""Application service for secure immutable document capture."""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from tempfile import SpooledTemporaryFile
from typing import Final
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
    ObjectStorage,
    ObjectStorageError,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import (
    M0_0_PROCESSING_PROFILE,
    ProcessingProfile,
    create_captured_processing_run,
)

PDF_HEADER_PATTERN: Final = re.compile(rb"%PDF-(?:1\.[0-9]|2\.0)")
PDF_HEADER_LENGTH: Final = 8
READ_CHUNK_SIZE: Final = 1024 * 1024
M0_0_DOCUMENT_TYPE: Final = "earnings_brief"

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


class CaptureDocumentService:
    """Validate and atomically record one immutable document capture."""

    def __init__(
        self,
        *,
        object_storage: ObjectStorage,
        unit_of_work_factory: CaptureUnitOfWorkFactory,
        max_upload_bytes: int,
        processing_profile: ProcessingProfile = M0_0_PROCESSING_PROFILE,
        clock: Clock = lambda: datetime.now(UTC),
        identifier_factory: IdentifierFactory = uuid4,
    ) -> None:
        if max_upload_bytes < PDF_HEADER_LENGTH:
            raise ValueError(f"max_upload_bytes must be at least {PDF_HEADER_LENGTH} bytes")
        self._object_storage = object_storage
        self._unit_of_work_factory = unit_of_work_factory
        self._max_upload_bytes = max_upload_bytes
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
            return self._record_capture(
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
