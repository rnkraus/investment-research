"""Provider-neutral S3 adapter for immutable canonical originals."""

from base64 import b64encode
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, NoReturn, Protocol, cast

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from investment_research.infrastructure.logging import application_logger
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
DEFAULT_UPLOAD_SPOOL_MAX_BYTES = 8 * 1024 * 1024
IMMUTABLE_CONFLICT_CODES = frozenset({"ConditionalRequestConflict", "PreconditionFailed"})
UNAVAILABLE_OBJECT_CODES = frozenset({"AccessDenied", "NoSuchBucket", "NoSuchKey", "NotFound"})

_logger = application_logger("object_storage")


class S3Client(Protocol):
    """Small dynamic-client surface used by the application-owned adapter."""

    def head_bucket(self, **kwargs: object) -> Mapping[str, object]:
        """Verify access to one configured bucket."""

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        """Create one S3 object."""

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        """Open one S3 object."""

    def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        """Delete one S3 object."""


def create_s3_client(
    *,
    endpoint_url: str | None,
    region_name: str,
    access_key_id: str | None,
    secret_access_key: str | None,
) -> S3Client:
    """Create a bounded synchronous S3 client for a local or managed endpoint."""

    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region_name,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=30,
            retries={"mode": "standard", "total_max_attempts": 3},
            s3={"addressing_style": "path"},
            tcp_keepalive=True,
        ),
    )
    return cast(S3Client, client)


@dataclass(frozen=True, slots=True)
class S3ObjectStorageReadinessProbe:
    """Check access to the configured canonical-original bucket."""

    client: S3Client
    bucket: str

    def is_ready(self) -> bool:
        """Return false without exposing endpoint, bucket, or credential details."""

        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (BotoCoreError, ClientError) as error:
            _logger.warning(
                "health.object_storage.unavailable",
                extra={"exception_type": type(error).__name__},
            )
            return False
        return True


class S3ObjectStorage:
    """Store immutable workspace-scoped objects through the S3 data plane."""

    def __init__(
        self,
        *,
        client: S3Client,
        bucket: str,
        upload_spool_max_bytes: int = DEFAULT_UPLOAD_SPOOL_MAX_BYTES,
    ) -> None:
        if not bucket or bucket != bucket.strip():
            raise ValueError("bucket must be a non-empty normalized name")
        if upload_spool_max_bytes < 0:
            raise ValueError("upload_spool_max_bytes must not be negative")
        self._client = client
        self._bucket = bucket
        self._upload_spool_max_bytes = upload_spool_max_bytes

    def store_immutable(
        self,
        context: WorkspaceContext,
        storage_key: StorageKey,
        content: BinaryReadable,
    ) -> StoredObject:
        """Conditionally create one object while computing its trusted metadata."""

        key = self._validate_key(context, storage_key, hide_scope=False)
        digest = sha256()
        byte_size = 0

        with SpooledTemporaryFile[bytes](
            max_size=self._upload_spool_max_bytes,
            mode="w+b",
        ) as prepared:
            while chunk := content.read(READ_CHUNK_SIZE):
                if not isinstance(chunk, bytes):
                    raise TypeError("object content must be opened in binary mode")
                prepared.write(chunk)
                digest.update(chunk)
                byte_size += len(chunk)
            prepared.seek(0)

            try:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=prepared,
                    ContentLength=byte_size,
                    ChecksumAlgorithm="SHA256",
                    ChecksumSHA256=b64encode(digest.digest()).decode("ascii"),
                    IfNoneMatch="*",
                    Metadata={"checksum-sha256": digest.hexdigest()},
                )
            except ClientError as error:
                if self._error_code(error) in IMMUTABLE_CONFLICT_CODES:
                    raise ObjectAlreadyExistsError("immutable object already exists") from error
                raise ObjectStorageError("object storage request failed") from error
            except BotoCoreError as error:
                raise ObjectStorageError("object storage request failed") from error

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
        """Open one scoped object while hiding all unavailable-object distinctions."""

        key = self._validate_key(context, storage_key, hide_scope=True)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if self._is_unavailable(error):
                raise ObjectNotFoundError("object is unavailable") from error
            raise ObjectStorageError("object storage request failed") from error
        except BotoCoreError as error:
            raise ObjectStorageError("object storage request failed") from error

        body = response.get("Body")
        if not callable(getattr(body, "read", None)) or not callable(getattr(body, "close", None)):
            raise ObjectStorageError("object storage returned an invalid response body")

        readable_body = cast(BinaryIO, body)
        try:
            yield readable_body
        finally:
            readable_body.close()

    def discard_uncommitted(
        self,
        context: WorkspaceContext,
        storage_key: StorageKey,
    ) -> None:
        """Idempotently remove only the exact in-scope uncommitted object."""

        key = self._validate_key(context, storage_key, hide_scope=False)
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if self._error_code(error) in {"NoSuchBucket", "NoSuchKey", "NotFound"}:
                return
            raise ObjectStorageError("object storage request failed") from error
        except BotoCoreError as error:
            raise ObjectStorageError("object storage request failed") from error

    @staticmethod
    def _validate_key(
        context: WorkspaceContext,
        storage_key: StorageKey,
        *,
        hide_scope: bool,
    ) -> str:
        """Require the same canonical workspace prefix as every storage adapter."""

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
            S3ObjectStorage._raise_scope_error(hide_scope)
        return key

    @staticmethod
    def _is_unavailable(error: ClientError) -> bool:
        """Collapse authorization and absence into one non-disclosing read result."""

        return S3ObjectStorage._error_code(
            error
        ) in UNAVAILABLE_OBJECT_CODES or S3ObjectStorage._http_status(error) in {403, 404}

    @staticmethod
    def _error_code(error: ClientError) -> str | None:
        """Extract one normalized AWS error code from an untrusted SDK response."""

        response = cast(Mapping[str, object], error.response)
        error_details = response.get("Error")
        if not isinstance(error_details, Mapping):
            return None
        code = error_details.get("Code")
        return code if isinstance(code, str) else None

    @staticmethod
    def _http_status(error: ClientError) -> int | None:
        """Extract one HTTP status without assuming a complete SDK error shape."""

        response = cast(Mapping[str, object], error.response)
        metadata = response.get("ResponseMetadata")
        if not isinstance(metadata, Mapping):
            return None
        status = metadata.get("HTTPStatusCode")
        return status if isinstance(status, int) else None

    @staticmethod
    def _raise_scope_error(hide_scope: bool) -> NoReturn:
        """Expose a precise write error but hide invalid or foreign read keys."""

        if hide_scope:
            raise ObjectNotFoundError("object is unavailable")
        raise ObjectStorageScopeError("object key is outside the active workspace")
