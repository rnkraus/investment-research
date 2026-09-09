"""Immutable local object storage for the M0.0 development environment."""

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from errno import ELOOP, ENOTDIR
from hashlib import sha256
from pathlib import Path, PurePosixPath
from secrets import token_hex
from stat import S_ISREG
from typing import BinaryIO, NoReturn

from investment_research.modules.documents.domain import StorageKey
from investment_research.modules.documents.ports import (
    BinaryReadable,
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStorageScopeError,
    StoredObject,
)
from investment_research.modules.identity.domain import WorkspaceContext

READ_CHUNK_SIZE = 1024 * 1024


class LocalObjectStorage:
    """Store immutable objects below one private local filesystem root."""

    def __init__(self, root: Path) -> None:
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise RuntimeError("local object storage requires secure POSIX file operations")
        self._root = root.expanduser().resolve(strict=False)

    def store_immutable(
        self,
        context: WorkspaceContext,
        storage_key: StorageKey,
        content: BinaryReadable,
    ) -> StoredObject:
        """Write content completely before atomically publishing a new object."""

        parts = self._validate_key(context, storage_key, hide_scope=False)
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        digest = sha256()
        byte_size = 0

        with self._open_parent_directory(
            parts,
            create=True,
            hide_scope=False,
        ) as (parent_descriptor, object_name):
            temporary_name, descriptor = self._create_temporary_file(
                parent_descriptor,
                object_name,
            )
            try:
                with os.fdopen(descriptor, "wb") as temporary_file:
                    while chunk := content.read(READ_CHUNK_SIZE):
                        if not isinstance(chunk, bytes):
                            raise TypeError("object content must be opened in binary mode")
                        temporary_file.write(chunk)
                        digest.update(chunk)
                        byte_size += len(chunk)
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())

                try:
                    os.link(
                        temporary_name,
                        object_name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileExistsError as error:
                    raise ObjectAlreadyExistsError("immutable object already exists") from error
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=parent_descriptor)

        return StoredObject(
            storage_key=storage_key,
            byte_size=byte_size,
            checksum_sha256=digest.hexdigest(),
        )

    @contextmanager
    def open(
        self,
        context: WorkspaceContext,
        storage_key: StorageKey,
    ) -> Iterator[BinaryIO]:
        """Open one scoped object while hiding invalid, foreign, and missing keys alike."""

        try:
            parts = self._validate_key(context, storage_key, hide_scope=True)
            with self._open_parent_directory(
                parts,
                create=False,
                hide_scope=True,
            ) as (parent_descriptor, object_name):
                descriptor = os.open(
                    object_name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=parent_descriptor,
                )
                if not S_ISREG(os.fstat(descriptor).st_mode):
                    os.close(descriptor)
                    raise OSError("stored object is not a regular file")
        except OSError as error:
            raise ObjectNotFoundError("object is unavailable") from error

        with os.fdopen(descriptor, "rb") as stored_file:
            yield stored_file

    def discard_uncommitted(
        self,
        context: WorkspaceContext,
        storage_key: StorageKey,
    ) -> None:
        """Remove only an in-scope object whose canonical transaction did not commit."""

        parts = self._validate_key(context, storage_key, hide_scope=False)
        try:
            with self._open_parent_directory(
                parts,
                create=False,
                hide_scope=False,
            ) as (parent_descriptor, object_name):
                descriptor = os.open(
                    object_name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=parent_descriptor,
                )
                try:
                    if not S_ISREG(os.fstat(descriptor).st_mode):
                        raise OSError("stored object is not a regular file")
                finally:
                    os.close(descriptor)
                os.unlink(object_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            return
        except OSError as error:
            if error.errno in {ELOOP, ENOTDIR}:
                self._raise_scope_error(hide_scope=False)
            raise

    def _validate_key(
        self,
        context: WorkspaceContext,
        storage_key: StorageKey,
        *,
        hide_scope: bool,
    ) -> tuple[str, ...]:
        """Validate a canonical key without resolving any filesystem link."""

        key = str(storage_key)
        pure_key = PurePosixPath(key)
        parts = pure_key.parts
        expected_prefix = ("workspaces", str(context.workspace_id))
        is_valid = (
            bool(key)
            and not pure_key.is_absolute()
            and "\\" not in key
            and str(pure_key) == key
            and len(parts) > len(expected_prefix)
            and parts[: len(expected_prefix)] == expected_prefix
            and "." not in parts
            and ".." not in parts
        )
        if not is_valid:
            self._raise_scope_error(hide_scope)
        return parts

    @contextmanager
    def _open_parent_directory(
        self,
        parts: tuple[str, ...],
        *,
        create: bool,
        hide_scope: bool,
    ) -> Iterator[tuple[int, str]]:
        """Traverse every directory by descriptor without following symbolic links."""

        descriptors: list[int] = []
        try:
            root_descriptor = os.open(
                self._root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            descriptors.append(root_descriptor)
            current_descriptor = root_descriptor
            for component in parts[:-1]:
                child_descriptor = self._open_child_directory(
                    current_descriptor,
                    component,
                    create=create,
                    hide_scope=hide_scope,
                )
                descriptors.append(child_descriptor)
                current_descriptor = child_descriptor
            yield current_descriptor, parts[-1]
        except OSError as error:
            if error.errno in {ELOOP, ENOTDIR}:
                self._raise_scope_error(hide_scope)
            raise
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _open_child_directory(
        self,
        parent_descriptor: int,
        component: str,
        *,
        create: bool,
        hide_scope: bool,
    ) -> int:
        """Open or create one real directory below an already trusted descriptor."""

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            return os.open(component, flags, dir_fd=parent_descriptor)
        except FileNotFoundError:
            if not create:
                raise
            with suppress(FileExistsError):
                os.mkdir(component, mode=0o700, dir_fd=parent_descriptor)
            try:
                return os.open(component, flags, dir_fd=parent_descriptor)
            except OSError as error:
                if error.errno in {ELOOP, ENOTDIR}:
                    self._raise_scope_error(hide_scope)
                raise
        except OSError as error:
            if error.errno in {ELOOP, ENOTDIR}:
                self._raise_scope_error(hide_scope)
            raise

    @staticmethod
    def _create_temporary_file(
        parent_descriptor: int,
        object_name: str,
    ) -> tuple[str, int]:
        """Create a private sibling used for atomic immutable publication."""

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        for _ in range(100):
            temporary_name = f".{object_name}.{token_hex(16)}.tmp"
            try:
                descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                continue
            return temporary_name, descriptor
        raise ObjectStorageError("could not allocate a temporary object")

    @staticmethod
    def _raise_scope_error(hide_scope: bool) -> NoReturn:
        """Use a non-disclosing read error and a specific pre-write scope error."""

        if hide_scope:
            raise ObjectNotFoundError("object is unavailable")
        raise ObjectStorageScopeError("object key is outside the active workspace")
