"""Unit tests for immutable workspace-scoped local object storage."""

from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest

from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.modules.documents.ports import (
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectStorage,
    ObjectStorageScopeError,
)
from investment_research.modules.identity.domain import WorkspaceContext

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
OTHER_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
ORIGINAL_ID = UUID("10000000-0000-4000-8000-000000000003")
PDF_CONTENT = b"%PDF-1.7\nsynthetic immutable test document\n%%EOF\n"
PDF_SHA256 = "f78657e3af4cc19b31533a48cfd30331fd081857577c8f835eabb88ee00de889"


class InterruptedBytesIO(BytesIO):
    """Raise after one partial read to simulate an interrupted upload."""

    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self._read_count = 0

    def read(self, size: int | None = -1) -> bytes:
        self._read_count += 1
        if self._read_count > 1:
            raise OSError("simulated interrupted read")
        return super().read(8 if size is None or size < 0 else min(size, 8))


def make_context(workspace_id: UUID = WORKSPACE_ID) -> WorkspaceContext:
    """Build one trusted workspace context for storage tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=workspace_id)


def original_key(workspace_id: UUID = WORKSPACE_ID) -> str:
    """Return the canonical server-owned key shape for one PDF original."""

    return f"workspaces/{workspace_id}/originals/{ORIGINAL_ID}.pdf"


def test_local_adapter_matches_object_storage_protocol(tmp_path: Path) -> None:
    storage: ObjectStorage = LocalObjectStorage(tmp_path)

    assert isinstance(storage, LocalObjectStorage)


def test_store_computes_metadata_and_open_returns_identical_bytes(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    context = make_context()
    key = original_key()

    result = storage.store_immutable(context, key, BytesIO(PDF_CONTENT))

    assert result.storage_key == key
    assert result.byte_size == len(PDF_CONTENT)
    assert result.checksum_sha256 == PDF_SHA256
    with storage.open(context, key) as stored_file:
        assert stored_file.read() == PDF_CONTENT
    assert list(tmp_path.rglob("*.tmp")) == []


def test_store_never_overwrites_an_existing_object(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    context = make_context()
    key = original_key()
    storage.store_immutable(context, key, BytesIO(PDF_CONTENT))

    with pytest.raises(ObjectAlreadyExistsError, match="already exists"):
        storage.store_immutable(context, key, BytesIO(b"replacement"))

    with storage.open(context, key) as stored_file:
        assert stored_file.read() == PDF_CONTENT


def test_discard_uncommitted_removes_object_idempotently(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    context = make_context()
    key = original_key()
    storage.store_immutable(context, key, BytesIO(PDF_CONTENT))

    storage.discard_uncommitted(context, key)
    storage.discard_uncommitted(context, key)

    with pytest.raises(ObjectNotFoundError), storage.open(context, key):
        pytest.fail("a discarded uncommitted object must not remain available")


def test_permanent_cleanup_preserves_other_objects_and_is_idempotent(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    context = make_context()
    key = original_key()
    storage.store_immutable(context, key, BytesIO(PDF_CONTENT))
    storage.store_immutable(context, key + ".other", BytesIO(b"preserve"))
    storage.delete_original(context, key)
    storage.delete_original(context, key)
    with pytest.raises(ObjectNotFoundError), storage.open(context, key):
        pytest.fail("a purged original must not remain available")
    with storage.open(context, key + ".other") as content:
        assert content.read() == b"preserve"


def test_permanent_cleanup_rejects_symlink_and_foreign_workspace(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    context = make_context()
    key = original_key()
    target = tmp_path / "unrelated.txt"
    target.write_bytes(b"preserve")
    link = tmp_path / key
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    with pytest.raises(ObjectStorageScopeError):
        storage.delete_original(context, key)
    with pytest.raises(ObjectStorageScopeError):
        storage.delete_original(context, original_key(OTHER_WORKSPACE_ID))
    assert target.read_bytes() == b"preserve"


@pytest.mark.parametrize(
    "unsafe_key",
    [
        f"workspaces/{OTHER_WORKSPACE_ID}/originals/{ORIGINAL_ID}.pdf",
        f"workspaces/{WORKSPACE_ID}/../private/{ORIGINAL_ID}.pdf",
        f"workspaces/{WORKSPACE_ID}/originals\\{ORIGINAL_ID}.pdf",
        f"/workspaces/{WORKSPACE_ID}/originals/{ORIGINAL_ID}.pdf",
    ],
)
def test_store_rejects_foreign_or_unsafe_keys(tmp_path: Path, unsafe_key: str) -> None:
    storage = LocalObjectStorage(tmp_path)

    with pytest.raises(ObjectStorageScopeError, match="outside the active workspace"):
        storage.store_immutable(make_context(), unsafe_key, BytesIO(PDF_CONTENT))

    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize(
    "unavailable_key",
    [
        f"workspaces/{WORKSPACE_ID}/originals/missing.pdf",
        f"workspaces/{OTHER_WORKSPACE_ID}/originals/{ORIGINAL_ID}.pdf",
        f"workspaces/{WORKSPACE_ID}/../private/{ORIGINAL_ID}.pdf",
    ],
)
def test_open_hides_missing_foreign_and_unsafe_keys(
    tmp_path: Path,
    unavailable_key: str,
) -> None:
    storage = LocalObjectStorage(tmp_path)

    with (
        pytest.raises(ObjectNotFoundError, match="object is unavailable"),
        storage.open(make_context(), unavailable_key),
    ):
        pytest.fail("an unavailable object must never be opened")


def test_interrupted_store_leaves_no_published_or_temporary_object(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    context = make_context()
    key = original_key()

    with pytest.raises(OSError, match="interrupted read"):
        storage.store_immutable(context, key, InterruptedBytesIO(PDF_CONTENT))

    with pytest.raises(ObjectNotFoundError), storage.open(context, key):
        pytest.fail("an interrupted object must never be opened")
    assert list(tmp_path.rglob("*.tmp")) == []


def test_open_refuses_a_symbolic_link_object(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    context = make_context()
    key = original_key()
    external_file = tmp_path.parent / "external-object.pdf"
    external_file.write_bytes(PDF_CONTENT)
    object_path = tmp_path / key
    object_path.parent.mkdir(parents=True)
    object_path.symlink_to(external_file)

    with (
        pytest.raises(ObjectNotFoundError, match="object is unavailable"),
        storage.open(context, key),
    ):
        pytest.fail("a symbolic link must never be opened")


def test_open_refuses_a_symbolic_link_to_another_workspace(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    context = make_context()
    key = original_key()
    other_workspace = tmp_path / "workspaces" / str(OTHER_WORKSPACE_ID)
    other_originals = other_workspace / "originals"
    other_originals.mkdir(parents=True)
    (other_originals / f"{ORIGINAL_ID}.pdf").write_bytes(PDF_CONTENT)
    workspace_link = tmp_path / "workspaces" / str(WORKSPACE_ID)
    workspace_link.symlink_to(other_workspace, target_is_directory=True)

    with (
        pytest.raises(ObjectNotFoundError, match="object is unavailable"),
        storage.open(context, key),
    ):
        pytest.fail("a workspace directory link must never expose another workspace")


def test_store_refuses_a_symbolic_link_parent_directory(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    context = make_context()
    key = original_key()
    external_directory = tmp_path / "outside-workspace"
    external_directory.mkdir()
    workspace_directory = tmp_path / "workspaces" / str(WORKSPACE_ID)
    workspace_directory.parent.mkdir(parents=True)
    workspace_directory.symlink_to(external_directory, target_is_directory=True)

    with pytest.raises(ObjectStorageScopeError, match="outside the active workspace"):
        storage.store_immutable(context, key, BytesIO(PDF_CONTENT))

    assert list(external_directory.rglob("*")) == []
