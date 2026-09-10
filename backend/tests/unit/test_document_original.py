"""Unit tests for authorized immutable-original retrieval."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest

from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.modules.documents.application import (
    OpenOriginalService,
    OriginalIntegrityError,
    OriginalUnavailableError,
)
from investment_research.modules.documents.domain import OriginalArtifact, OriginalMediaType
from investment_research.modules.documents.ports import (
    DocumentReadUnitOfWork,
    DocumentReadUnitOfWorkFactory,
    DocumentRepository,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.ports import ProcessingOriginalProvider

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000002")
ORIGINAL_ID = UUID("10000000-0000-4000-8000-000000000003")
CAPTURED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
PDF_CONTENT = b"%PDF-1.7\nsynthetic original test\n%%EOF\n"


class RecordingReadUnitOfWork:
    """Expose one mocked document repository through the read contract."""

    def __init__(self) -> None:
        self.document_mock = create_autospec(DocumentRepository, instance=True)
        self.documents = cast(DocumentRepository, self.document_mock)
        self.enter_count = 0
        self.exit_count = 0

    @contextmanager
    def open(self) -> Iterator[DocumentReadUnitOfWork]:
        """Record entry and closure around one repository query."""

        self.enter_count += 1
        try:
            yield self
        finally:
            self.exit_count += 1


def make_context() -> WorkspaceContext:
    """Return the trusted local workspace used by original tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_original(content: bytes = PDF_CONTENT) -> OriginalArtifact:
    """Build canonical metadata matching one stored test object."""

    return OriginalArtifact(
        id=ORIGINAL_ID,
        workspace_id=WORKSPACE_ID,
        document_version_id=VERSION_ID,
        storage_key=f"workspaces/{WORKSPACE_ID}/originals/{ORIGINAL_ID}.pdf",
        media_type=OriginalMediaType.PDF,
        byte_size=len(content),
        checksum_sha256=sha256(content).hexdigest(),
        captured_at=CAPTURED_AT,
    )


def make_service(
    tmp_path: Path,
    unit_of_work: RecordingReadUnitOfWork,
    original: OriginalArtifact | None,
    *,
    stored_content: bytes | None = PDF_CONTENT,
) -> OpenOriginalService:
    """Build a service with real scoped storage and controlled persistence."""

    storage = LocalObjectStorage(tmp_path / "objects")
    unit_of_work.document_mock.get_original_artifact.return_value = original
    unit_of_work.document_mock.get_original_artifact_by_version_id.return_value = original
    if original is not None and stored_content is not None:
        storage.store_immutable(
            make_context(),
            original.storage_key,
            BytesIO(stored_content),
        )
    factory: DocumentReadUnitOfWorkFactory = unit_of_work.open
    return OpenOriginalService(
        object_storage=storage,
        unit_of_work_factory=factory,
    )


def test_open_original_returns_verified_pdf_and_safe_metadata(tmp_path: Path) -> None:
    unit_of_work = RecordingReadUnitOfWork()
    original = make_original()
    service = make_service(tmp_path, unit_of_work, original)

    download = service.open_original(make_context(), DOCUMENT_ID, VERSION_ID)

    assert b"".join(download.iter_bytes()) == PDF_CONTENT
    assert download.media_type is OriginalMediaType.PDF
    assert download.byte_size == len(PDF_CONTENT)
    assert download.filename == f"document-{DOCUMENT_ID}-version-{VERSION_ID}.pdf"
    assert unit_of_work.enter_count == unit_of_work.exit_count == 1
    unit_of_work.document_mock.get_original_artifact.assert_called_once_with(
        make_context(),
        DOCUMENT_ID,
        VERSION_ID,
    )


def test_open_for_processing_yields_verified_version_stream_and_closes_it(
    tmp_path: Path,
) -> None:
    unit_of_work = RecordingReadUnitOfWork()
    original = make_original()
    service: ProcessingOriginalProvider = make_service(tmp_path, unit_of_work, original)

    with service.open_for_processing(make_context(), VERSION_ID) as content:
        assert content.read() == PDF_CONTENT
        assert content.closed is False

    assert content.closed is True
    assert unit_of_work.enter_count == unit_of_work.exit_count == 1
    unit_of_work.document_mock.get_original_artifact_by_version_id.assert_called_once_with(
        make_context(),
        VERSION_ID,
    )


def test_open_original_hides_unavailable_document_before_storage(tmp_path: Path) -> None:
    unit_of_work = RecordingReadUnitOfWork()
    service = make_service(tmp_path, unit_of_work, None, stored_content=None)

    with pytest.raises(OriginalUnavailableError, match="unavailable"):
        service.open_original(make_context(), DOCUMENT_ID, VERSION_ID)

    assert unit_of_work.enter_count == unit_of_work.exit_count == 1
    assert not (tmp_path / "objects").exists()


def test_open_for_processing_hides_unavailable_version_before_storage(tmp_path: Path) -> None:
    unit_of_work = RecordingReadUnitOfWork()
    service = make_service(tmp_path, unit_of_work, None, stored_content=None)

    with (
        pytest.raises(OriginalUnavailableError, match="original is unavailable"),
        service.open_for_processing(make_context(), VERSION_ID),
    ):
        pytest.fail("an unavailable processing original must never yield content")

    assert unit_of_work.enter_count == unit_of_work.exit_count == 1
    assert not (tmp_path / "objects").exists()


def test_open_original_rejects_missing_stored_object(tmp_path: Path) -> None:
    unit_of_work = RecordingReadUnitOfWork()
    service = make_service(
        tmp_path,
        unit_of_work,
        make_original(),
        stored_content=None,
    )

    with pytest.raises(OriginalIntegrityError, match="unavailable"):
        service.open_original(make_context(), DOCUMENT_ID, VERSION_ID)


def test_open_original_rejects_content_that_differs_from_canonical_checksum(
    tmp_path: Path,
) -> None:
    unit_of_work = RecordingReadUnitOfWork()
    different_content = b"%PDF-1.7\nsynthetic original TEST\n%%EOF\n"
    assert len(different_content) == len(PDF_CONTENT)
    service = make_service(
        tmp_path,
        unit_of_work,
        make_original(),
        stored_content=different_content,
    )

    with pytest.raises(OriginalIntegrityError, match="canonical metadata"):
        service.open_original(make_context(), DOCUMENT_ID, VERSION_ID)


def test_original_stream_closes_when_consumer_stops_early(tmp_path: Path) -> None:
    unit_of_work = RecordingReadUnitOfWork()
    service = make_service(tmp_path, unit_of_work, make_original())
    download = service.open_original(make_context(), DOCUMENT_ID, VERSION_ID)
    chunks = download.iter_bytes()

    assert next(chunks) == PDF_CONTENT
    chunks.close()

    assert download._content.closed
