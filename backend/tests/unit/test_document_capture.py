"""Unit tests for secure document capture orchestration."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from logging import Logger
from pathlib import Path
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.modules.documents.application import (
    CaptureDocumentService,
    InvalidPdfSignatureError,
    UnsupportedDocumentTypeError,
    UploadTooLargeError,
)
from investment_research.modules.documents.contracts import CaptureDocumentInput
from investment_research.modules.documents.ports import (
    CaptureUnitOfWork,
    CaptureUnitOfWorkFactory,
    DocumentRepository,
    ObjectNotFoundError,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.ports import (
    ProcessingDispatcher,
    ProcessingDispatchError,
    ProcessingRepository,
)

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000002")
ORIGINAL_ID = UUID("10000000-0000-4000-8000-000000000003")
PROCESSING_RUN_ID = UUID("10000000-0000-4000-8000-000000000004")
CAPTURED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
PDF_CONTENT = b"%PDF-1.7\nsynthetic capture test\n%%EOF\n"
PDF_SHA256 = sha256(PDF_CONTENT).hexdigest()


class RecordingUnitOfWork:
    """Expose mocked repositories through the application-owned transaction contract."""

    def __init__(self) -> None:
        self.document_mock = create_autospec(DocumentRepository, instance=True)
        self.processing_mock = create_autospec(ProcessingRepository, instance=True)
        self.documents = cast(DocumentRepository, self.document_mock)
        self.processing = cast(ProcessingRepository, self.processing_mock)
        self.enter_count = 0
        self.is_open = False

    @contextmanager
    def open(self) -> Iterator[CaptureUnitOfWork]:
        """Record transaction entry while yielding the shared repository pair."""

        self.enter_count += 1
        self.is_open = True
        try:
            yield self
        finally:
            self.is_open = False


class RecordingProcessingDispatcher:
    """Record publication order and optionally simulate one provider failure."""

    def __init__(
        self,
        unit_of_work: RecordingUnitOfWork,
        *,
        error: Exception | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._error = error
        self.jobs: list[ProcessingJobV1] = []
        self.transaction_states: list[bool] = []

    def dispatch(self, job: ProcessingJobV1) -> None:
        """Record the job and whether capture still owns an open transaction."""

        self.jobs.append(job)
        self.transaction_states.append(self._unit_of_work.is_open)
        if self._error is not None:
            raise self._error


def make_context() -> WorkspaceContext:
    """Build the trusted workspace used by capture tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_input(
    *,
    filename: str = "earnings-brief.pdf",
    media_type: str = "application/pdf",
) -> CaptureDocumentInput:
    """Build valid synthetic capture metadata with optional type overrides."""

    return CaptureDocumentInput(
        filename=filename,
        media_type=media_type,
        title="Northstar Grid Analytics FY2025 Earnings Brief",
        language="en",
        published_at=datetime(2026, 2, 19, tzinfo=UTC),
        available_at=datetime(2026, 2, 19, tzinfo=UTC),
    )


def make_service(
    tmp_path: Path,
    unit_of_work: RecordingUnitOfWork,
    *,
    max_upload_bytes: int = 1024,
    processing_dispatcher: ProcessingDispatcher | None = None,
) -> CaptureDocumentService:
    """Build a deterministic service with real local storage and fake persistence."""

    identifiers = iter((DOCUMENT_ID, VERSION_ID, ORIGINAL_ID, PROCESSING_RUN_ID))
    unit_of_work_factory: CaptureUnitOfWorkFactory = unit_of_work.open
    return CaptureDocumentService(
        object_storage=LocalObjectStorage(tmp_path / "objects"),
        unit_of_work_factory=unit_of_work_factory,
        max_upload_bytes=max_upload_bytes,
        processing_dispatcher=processing_dispatcher,
        clock=lambda: CAPTURED_AT,
        identifier_factory=identifiers.__next__,
    )


def test_capture_preserves_pdf_and_stages_consistent_canonical_records(tmp_path: Path) -> None:
    unit_of_work = RecordingUnitOfWork()
    dispatcher = RecordingProcessingDispatcher(unit_of_work)
    service = make_service(
        tmp_path,
        unit_of_work,
        processing_dispatcher=dispatcher,
    )

    result = service.capture(make_context(), make_input(), BytesIO(PDF_CONTENT))

    assert result.document_id == DOCUMENT_ID
    assert result.document_version_id == VERSION_ID
    assert result.original_artifact_id == ORIGINAL_ID
    assert result.processing_run_id == PROCESSING_RUN_ID
    assert result.state is ProcessingState.CAPTURED
    assert unit_of_work.enter_count == 1

    unit_of_work.document_mock.add_capture.assert_called_once()
    context, document, version, original = unit_of_work.document_mock.add_capture.call_args.args
    assert context == make_context()
    assert document.current_version_id == version.id == VERSION_ID
    assert version.document_id == document.id == DOCUMENT_ID
    assert original.document_version_id == version.id
    assert version.content_sha256 == original.checksum_sha256 == PDF_SHA256
    assert original.byte_size == len(PDF_CONTENT)
    assert original.storage_key == f"workspaces/{WORKSPACE_ID}/originals/{ORIGINAL_ID}.pdf"

    unit_of_work.processing_mock.add_run.assert_called_once()
    run_context, processing_run = unit_of_work.processing_mock.add_run.call_args.args
    assert run_context == make_context()
    assert processing_run.id == PROCESSING_RUN_ID
    assert processing_run.document_version_id == VERSION_ID
    assert processing_run.state is ProcessingState.CAPTURED
    assert processing_run.created_at == CAPTURED_AT
    assert dispatcher.jobs == [
        ProcessingJobV1.create(
            workspace_id=WORKSPACE_ID,
            processing_run_id=PROCESSING_RUN_ID,
        )
    ]
    assert dispatcher.transaction_states == [False]

    storage = LocalObjectStorage(tmp_path / "objects")
    with storage.open(make_context(), original.storage_key) as stored_file:
        assert stored_file.read() == PDF_CONTENT


def test_capture_discards_stored_object_when_canonical_transaction_fails(
    tmp_path: Path,
) -> None:
    unit_of_work = RecordingUnitOfWork()
    unit_of_work.processing_mock.add_run.side_effect = RuntimeError(
        "simulated canonical transaction failure"
    )
    dispatcher = RecordingProcessingDispatcher(unit_of_work)
    service = make_service(
        tmp_path,
        unit_of_work,
        processing_dispatcher=dispatcher,
    )

    with pytest.raises(RuntimeError, match="canonical transaction failure"):
        service.capture(make_context(), make_input(), BytesIO(PDF_CONTENT))

    unit_of_work.document_mock.add_capture.assert_called_once()
    unit_of_work.processing_mock.add_run.assert_called_once()
    assert dispatcher.jobs == []
    storage = LocalObjectStorage(tmp_path / "objects")
    storage_key = f"workspaces/{WORKSPACE_ID}/originals/{ORIGINAL_ID}.pdf"
    with (
        pytest.raises(ObjectNotFoundError, match="object is unavailable"),
        storage.open(make_context(), storage_key),
    ):
        pytest.fail("a failed capture must not leave a published object")


def test_capture_preserves_committed_state_when_dispatch_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit_of_work = RecordingUnitOfWork()
    dispatcher = RecordingProcessingDispatcher(
        unit_of_work,
        error=ProcessingDispatchError("synthetic private broker failure"),
    )
    logger = create_autospec(Logger, instance=True)
    monkeypatch.setattr(
        "investment_research.modules.documents.application.LOGGER",
        logger,
    )
    service = make_service(
        tmp_path,
        unit_of_work,
        processing_dispatcher=dispatcher,
    )

    result = service.capture(make_context(), make_input(), BytesIO(PDF_CONTENT))

    assert result.state is ProcessingState.CAPTURED
    assert dispatcher.transaction_states == [False]
    logger.warning.assert_called_once_with(
        "processing.dispatch.failed",
        extra={"workspace_id": WORKSPACE_ID, "job_id": PROCESSING_RUN_ID},
    )
    assert "broker failure" not in str(logger.warning.call_args)
    storage_key = f"workspaces/{WORKSPACE_ID}/originals/{ORIGINAL_ID}.pdf"
    with LocalObjectStorage(tmp_path / "objects").open(make_context(), storage_key) as stored_file:
        assert stored_file.read() == PDF_CONTENT


def test_capture_does_not_delete_committed_original_after_programming_error(
    tmp_path: Path,
) -> None:
    unit_of_work = RecordingUnitOfWork()
    dispatcher = RecordingProcessingDispatcher(
        unit_of_work,
        error=TypeError("synthetic programming error"),
    )
    service = make_service(
        tmp_path,
        unit_of_work,
        processing_dispatcher=dispatcher,
    )

    with pytest.raises(TypeError, match="synthetic programming error"):
        service.capture(make_context(), make_input(), BytesIO(PDF_CONTENT))

    assert dispatcher.transaction_states == [False]
    storage_key = f"workspaces/{WORKSPACE_ID}/originals/{ORIGINAL_ID}.pdf"
    with LocalObjectStorage(tmp_path / "objects").open(make_context(), storage_key) as stored_file:
        assert stored_file.read() == PDF_CONTENT


@pytest.mark.parametrize(
    ("filename", "media_type"),
    [
        pytest.param("earnings-brief.txt", "application/pdf", id="wrong-extension"),
        pytest.param("earnings-brief.pdf", "text/plain", id="wrong-media-type"),
    ],
)
def test_capture_rejects_unsupported_declared_type_before_storage(
    tmp_path: Path,
    filename: str,
    media_type: str,
) -> None:
    unit_of_work = RecordingUnitOfWork()
    service = make_service(tmp_path, unit_of_work)

    with pytest.raises(UnsupportedDocumentTypeError):
        service.capture(
            make_context(),
            make_input(filename=filename, media_type=media_type),
            BytesIO(PDF_CONTENT),
        )

    assert unit_of_work.enter_count == 0
    assert not (tmp_path / "objects").exists()


def test_capture_rejects_invalid_signature_without_publishing_object(tmp_path: Path) -> None:
    unit_of_work = RecordingUnitOfWork()
    service = make_service(tmp_path, unit_of_work)

    with pytest.raises(InvalidPdfSignatureError):
        service.capture(make_context(), make_input(), BytesIO(b"not a PDF"))

    assert unit_of_work.enter_count == 0
    assert not (tmp_path / "objects").exists()


def test_capture_rejects_oversized_pdf_without_publishing_object(tmp_path: Path) -> None:
    unit_of_work = RecordingUnitOfWork()
    service = make_service(
        tmp_path,
        unit_of_work,
        max_upload_bytes=len(PDF_CONTENT) - 1,
    )

    with pytest.raises(UploadTooLargeError, match="upload limit"):
        service.capture(make_context(), make_input(), BytesIO(PDF_CONTENT))

    assert unit_of_work.enter_count == 0
    assert not (tmp_path / "objects").exists()


def test_capture_accepts_pdf_at_exact_byte_limit(tmp_path: Path) -> None:
    unit_of_work = RecordingUnitOfWork()
    service = make_service(
        tmp_path,
        unit_of_work,
        max_upload_bytes=len(PDF_CONTENT),
    )

    result = service.capture(make_context(), make_input(), BytesIO(PDF_CONTENT))

    assert result.state is ProcessingState.CAPTURED
    assert unit_of_work.enter_count == 1


def test_capture_input_rejects_client_path_as_filename() -> None:
    with pytest.raises(ValidationError, match="filename"):
        make_input(filename="../earnings-brief.pdf")


def test_service_rejects_impossibly_small_limit() -> None:
    unit_of_work = RecordingUnitOfWork()
    unit_of_work_factory: CaptureUnitOfWorkFactory = unit_of_work.open

    with pytest.raises(ValueError, match="at least 8 bytes"):
        CaptureDocumentService(
            object_storage=LocalObjectStorage(Path("unused")),
            unit_of_work_factory=unit_of_work_factory,
            max_upload_bytes=7,
        )
