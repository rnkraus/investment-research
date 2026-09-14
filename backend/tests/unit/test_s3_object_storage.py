"""Unit tests for the provider-neutral immutable S3 adapter."""

from base64 import b64encode
from collections.abc import Mapping
from io import BytesIO
from typing import BinaryIO, cast
from uuid import UUID

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import (  # type: ignore[import-untyped]
    ClientError,
    EndpointConnectionError,
)

from investment_research.infrastructure.object_storage.s3 import (
    S3ObjectStorage,
    create_s3_client,
)
from investment_research.modules.documents.ports import (
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectStorage,
    ObjectStorageError,
    ObjectStorageScopeError,
)
from investment_research.modules.identity.domain import WorkspaceContext

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
OTHER_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
ORIGINAL_ID = UUID("10000000-0000-4000-8000-000000000003")
BUCKET = "investment-research-originals"
PDF_CONTENT = b"%PDF-1.7\nsynthetic immutable S3 test document\n%%EOF\n"
PDF_SHA256 = "03ad58444bc95412e19bcb409f36a4580667544e836fd13d585663279ec74770"


class InterruptedBytesIO(BytesIO):
    """Raise after one partial read to simulate an interrupted source stream."""

    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self._read_count = 0

    def read(self, size: int | None = -1) -> bytes:
        self._read_count += 1
        if self._read_count > 1:
            raise OSError("simulated interrupted read")
        return super().read(8 if size is None or size < 0 else min(size, 8))


class TrackingBytesIO(BytesIO):
    """Expose whether the adapter closed a downloaded response body."""


class FakeS3Client:
    """Capture the narrow S3 calls without performing network operations."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.next_put_error: BaseException | None = None
        self.next_get_error: BaseException | None = None
        self.next_delete_error: BaseException | None = None
        self.last_response_body: TrackingBytesIO | None = None

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        self.put_calls.append(kwargs)
        self._raise_once("next_put_error")
        bucket = cast(str, kwargs["Bucket"])
        key = cast(str, kwargs["Key"])
        body = cast(BinaryIO, kwargs["Body"])
        self.objects[(bucket, key)] = body.read()
        return {}

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        self.get_calls.append(kwargs)
        self._raise_once("next_get_error")
        bucket = cast(str, kwargs["Bucket"])
        key = cast(str, kwargs["Key"])
        try:
            content = self.objects[(bucket, key)]
        except KeyError as error:
            raise make_client_error("NoSuchKey", 404, "GetObject") from error
        self.last_response_body = TrackingBytesIO(content)
        return {"Body": self.last_response_body}

    def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        self.delete_calls.append(kwargs)
        self._raise_once("next_delete_error")
        bucket = cast(str, kwargs["Bucket"])
        key = cast(str, kwargs["Key"])
        self.objects.pop((bucket, key), None)
        return {}

    def _raise_once(self, attribute: str) -> None:
        error = cast(BaseException | None, getattr(self, attribute))
        setattr(self, attribute, None)
        if error is not None:
            raise error


def make_context(workspace_id: UUID = WORKSPACE_ID) -> WorkspaceContext:
    """Build one trusted workspace context for storage tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=workspace_id)


def original_key(workspace_id: UUID = WORKSPACE_ID) -> str:
    """Return the canonical server-owned key shape for one PDF original."""

    return f"workspaces/{workspace_id}/originals/{ORIGINAL_ID}.pdf"


def make_client_error(code: str, status: int, operation: str) -> ClientError:
    """Build the structured SDK error shape consumed by the adapter."""

    return ClientError(
        {
            "Error": {"Code": code, "Message": "synthetic SDK failure"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


def test_s3_adapter_matches_object_storage_protocol() -> None:
    storage: ObjectStorage = S3ObjectStorage(client=FakeS3Client(), bucket=BUCKET)

    assert isinstance(storage, S3ObjectStorage)


def test_store_sends_conditional_checksum_request_and_returns_metadata() -> None:
    client = FakeS3Client()
    storage = S3ObjectStorage(client=client, bucket=BUCKET, upload_spool_max_bytes=8)
    key = original_key()

    result = storage.store_immutable(make_context(), key, BytesIO(PDF_CONTENT))

    assert result.storage_key == key
    assert result.byte_size == len(PDF_CONTENT)
    assert result.checksum_sha256 == PDF_SHA256
    assert client.objects[(BUCKET, key)] == PDF_CONTENT
    assert len(client.put_calls) == 1
    request = client.put_calls[0]
    assert request["Bucket"] == BUCKET
    assert request["Key"] == key
    assert request["ContentLength"] == len(PDF_CONTENT)
    assert request["IfNoneMatch"] == "*"
    assert request["ChecksumAlgorithm"] == "SHA256"
    assert request["ChecksumSHA256"] == b64encode(bytes.fromhex(PDF_SHA256)).decode("ascii")
    assert request["Metadata"] == {"checksum-sha256": PDF_SHA256}


@pytest.mark.parametrize(
    ("code", "status"),
    [("PreconditionFailed", 412), ("ConditionalRequestConflict", 409)],
)
def test_store_maps_conditional_conflicts_without_retrying(code: str, status: int) -> None:
    client = FakeS3Client()
    client.next_put_error = make_client_error(code, status, "PutObject")
    storage = S3ObjectStorage(client=client, bucket=BUCKET)

    with pytest.raises(ObjectAlreadyExistsError, match="already exists"):
        storage.store_immutable(make_context(), original_key(), BytesIO(PDF_CONTENT))

    assert len(client.put_calls) == 1
    assert client.objects == {}


def test_store_maps_transport_failures_to_application_error() -> None:
    client = FakeS3Client()
    client.next_put_error = EndpointConnectionError(endpoint_url="http://storage.invalid")
    storage = S3ObjectStorage(client=client, bucket=BUCKET)

    with pytest.raises(ObjectStorageError, match="request failed"):
        storage.store_immutable(make_context(), original_key(), BytesIO(PDF_CONTENT))


def test_interrupted_source_never_reaches_s3() -> None:
    client = FakeS3Client()
    storage = S3ObjectStorage(client=client, bucket=BUCKET)

    with pytest.raises(OSError, match="interrupted read"):
        storage.store_immutable(make_context(), original_key(), InterruptedBytesIO(PDF_CONTENT))

    assert client.put_calls == []


@pytest.mark.parametrize(
    "unsafe_key",
    [
        f"workspaces/{OTHER_WORKSPACE_ID}/originals/{ORIGINAL_ID}.pdf",
        f"workspaces/{WORKSPACE_ID}/../private/{ORIGINAL_ID}.pdf",
        f"workspaces/{WORKSPACE_ID}/originals\\{ORIGINAL_ID}.pdf",
        f"/workspaces/{WORKSPACE_ID}/originals/{ORIGINAL_ID}.pdf",
    ],
)
def test_store_rejects_foreign_or_unsafe_keys_before_s3(unsafe_key: str) -> None:
    client = FakeS3Client()
    storage = S3ObjectStorage(client=client, bucket=BUCKET)

    with pytest.raises(ObjectStorageScopeError, match="outside the active workspace"):
        storage.store_immutable(make_context(), unsafe_key, BytesIO(PDF_CONTENT))

    assert client.put_calls == []


def test_open_returns_identical_bytes_and_closes_response_body() -> None:
    client = FakeS3Client()
    key = original_key()
    client.objects[(BUCKET, key)] = PDF_CONTENT
    storage = S3ObjectStorage(client=client, bucket=BUCKET)

    with storage.open(make_context(), key) as stored_file:
        assert stored_file.read() == PDF_CONTENT
        assert client.last_response_body is not None
        assert not client.last_response_body.closed

    assert client.last_response_body is not None
    assert client.last_response_body.closed


@pytest.mark.parametrize(
    ("code", "status"),
    [("AccessDenied", 403), ("NoSuchKey", 404), ("UnexpectedCode", 404)],
)
def test_open_hides_forbidden_and_missing_objects(code: str, status: int) -> None:
    client = FakeS3Client()
    client.next_get_error = make_client_error(code, status, "GetObject")
    storage = S3ObjectStorage(client=client, bucket=BUCKET)

    with (
        pytest.raises(ObjectNotFoundError, match="object is unavailable"),
        storage.open(make_context(), original_key()),
    ):
        pytest.fail("an unavailable object must never be opened")


def test_open_hides_foreign_key_before_s3() -> None:
    client = FakeS3Client()
    storage = S3ObjectStorage(client=client, bucket=BUCKET)

    with (
        pytest.raises(ObjectNotFoundError, match="object is unavailable"),
        storage.open(make_context(), original_key(OTHER_WORKSPACE_ID)),
    ):
        pytest.fail("a foreign object must never be queried")

    assert client.get_calls == []


def test_open_maps_unexpected_sdk_error_to_application_error() -> None:
    client = FakeS3Client()
    client.next_get_error = make_client_error("InternalError", 500, "GetObject")
    storage = S3ObjectStorage(client=client, bucket=BUCKET)

    with (
        pytest.raises(ObjectStorageError, match="request failed"),
        storage.open(make_context(), original_key()),
    ):
        pytest.fail("an unexpected SDK failure must not yield a body")


def test_discard_removes_exact_object_and_is_idempotent() -> None:
    client = FakeS3Client()
    key = original_key()
    client.objects[(BUCKET, key)] = PDF_CONTENT
    storage = S3ObjectStorage(client=client, bucket=BUCKET)

    storage.discard_uncommitted(make_context(), key)
    storage.discard_uncommitted(make_context(), key)

    assert client.objects == {}
    assert client.delete_calls == [
        {"Bucket": BUCKET, "Key": key},
        {"Bucket": BUCKET, "Key": key},
    ]


def test_discard_treats_explicit_missing_error_as_success() -> None:
    client = FakeS3Client()
    client.next_delete_error = make_client_error("NoSuchKey", 404, "DeleteObject")
    storage = S3ObjectStorage(client=client, bucket=BUCKET)

    storage.discard_uncommitted(make_context(), original_key())

    assert len(client.delete_calls) == 1


def test_discard_rejects_foreign_key_before_s3() -> None:
    client = FakeS3Client()
    storage = S3ObjectStorage(client=client, bucket=BUCKET)

    with pytest.raises(ObjectStorageScopeError, match="outside the active workspace"):
        storage.discard_uncommitted(make_context(), original_key(OTHER_WORKSPACE_ID))

    assert client.delete_calls == []


def test_create_client_applies_bounded_s3_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    expected_client = FakeS3Client()

    def fake_boto3_client(service_name: str, **kwargs: object) -> FakeS3Client:
        captured["service_name"] = service_name
        captured.update(kwargs)
        return expected_client

    monkeypatch.setattr(boto3, "client", fake_boto3_client)

    client = create_s3_client(
        endpoint_url="http://seaweedfs-s3:8333",
        region_name="us-east-1",
        access_key_id="synthetic-access-key",
        secret_access_key="synthetic-secret-key",
    )

    assert client is expected_client
    assert captured["service_name"] == "s3"
    assert captured["endpoint_url"] == "http://seaweedfs-s3:8333"
    assert captured["region_name"] == "us-east-1"
    assert captured["aws_access_key_id"] == "synthetic-access-key"
    assert captured["aws_secret_access_key"] == "synthetic-secret-key"
    config = cast(Config, captured["config"])
    assert config.signature_version == "s3v4"
    assert config.connect_timeout == 5
    assert config.read_timeout == 30
    assert config.retries == {"mode": "standard", "total_max_attempts": 3}
    assert config.s3 == {"addressing_style": "path"}
    assert config.tcp_keepalive is True


@pytest.mark.parametrize(("bucket", "spool_size"), [("", 1), (" bucket", 1), (BUCKET, -1)])
def test_constructor_rejects_invalid_configuration(bucket: str, spool_size: int) -> None:
    with pytest.raises(ValueError):
        S3ObjectStorage(
            client=FakeS3Client(),
            bucket=bucket,
            upload_spool_max_bytes=spool_size,
        )
