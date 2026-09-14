"""Unit tests for the idempotent SeaweedFS bucket bootstrap."""

from collections.abc import Mapping
from pathlib import Path

import pytest
from botocore.exceptions import (  # type: ignore[import-untyped]
    ClientError,
    EndpointConnectionError,
)

from investment_research.infrastructure.object_storage import seaweedfs_bootstrap
from investment_research.infrastructure.object_storage.seaweedfs_bootstrap import (
    ObjectStorageBootstrapError,
    ensure_bucket,
)


class FakeBootstrapClient:
    """Record bucket administration without opening a network connection."""

    def __init__(self, *, exists: bool) -> None:
        self.exists = exists
        self.head_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []
        self.next_head_error: BaseException | None = None
        self.next_create_error: BaseException | None = None

    def head_bucket(self, **kwargs: object) -> Mapping[str, object]:
        self.head_calls.append(kwargs)
        if self.next_head_error is not None:
            error = self.next_head_error
            self.next_head_error = None
            raise error
        if not self.exists:
            raise make_client_error(404, "HeadBucket")
        return {}

    def create_bucket(self, **kwargs: object) -> Mapping[str, object]:
        self.create_calls.append(kwargs)
        if self.next_create_error is not None:
            error = self.next_create_error
            self.next_create_error = None
            raise error
        self.exists = True
        return {}


def make_client_error(status: int, operation: str) -> ClientError:
    """Build one structured synthetic SDK error."""

    return ClientError(
        {
            "Error": {"Code": "SyntheticFailure", "Message": "synthetic SDK failure"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


def write_secret(path: Path, value: str) -> None:
    """Write a synthetic Docker-secret equivalent for one test."""

    path.write_text(value, encoding="utf-8")


def test_existing_bucket_is_left_unchanged() -> None:
    client = FakeBootstrapClient(exists=True)

    assert ensure_bucket(client, "investment-research-originals") is False
    assert client.head_calls == [{"Bucket": "investment-research-originals"}]
    assert client.create_calls == []


def test_missing_bucket_is_created_and_verified() -> None:
    client = FakeBootstrapClient(exists=False)

    assert ensure_bucket(client, "investment-research-originals") is True
    assert client.head_calls == [
        {"Bucket": "investment-research-originals"},
        {"Bucket": "investment-research-originals"},
    ]
    assert client.create_calls == [{"Bucket": "investment-research-originals"}]


def test_head_bucket_hides_non_missing_sdk_failure() -> None:
    client = FakeBootstrapClient(exists=True)
    client.next_head_error = make_client_error(403, "HeadBucket")

    with pytest.raises(ObjectStorageBootstrapError, match="bootstrap failed") as error:
        ensure_bucket(client, "investment-research-originals")

    assert "synthetic SDK failure" not in str(error.value)
    assert client.create_calls == []


def test_head_bucket_hides_transport_failure() -> None:
    client = FakeBootstrapClient(exists=True)
    client.next_head_error = EndpointConnectionError(endpoint_url="http://storage.invalid")

    with pytest.raises(ObjectStorageBootstrapError, match="bootstrap failed"):
        ensure_bucket(client, "investment-research-originals")


def test_create_bucket_failure_is_not_reported_as_success() -> None:
    client = FakeBootstrapClient(exists=False)
    client.next_create_error = make_client_error(500, "CreateBucket")

    with pytest.raises(ObjectStorageBootstrapError, match="bootstrap failed"):
        ensure_bucket(client, "investment-research-originals")


@pytest.mark.parametrize("value", ["", "\n"])
def test_empty_secret_is_rejected_without_exposing_content(tmp_path: Path, value: str) -> None:
    path = tmp_path / "secret"
    write_secret(path, value)

    with pytest.raises(ObjectStorageBootstrapError, match="secret is empty"):
        seaweedfs_bootstrap._read_secret(path, "administrator access key")


def test_missing_secret_has_safe_error(tmp_path: Path) -> None:
    with pytest.raises(ObjectStorageBootstrapError, match="secret is unavailable"):
        seaweedfs_bootstrap._read_secret(tmp_path / "missing", "administrator access key")


def test_main_loads_admin_secrets_and_bootstraps_configured_bucket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    access_key_path = tmp_path / "admin-access-key"
    secret_key_path = tmp_path / "admin-secret-key"
    write_secret(access_key_path, "synthetic-admin-access-key")
    write_secret(secret_key_path, "synthetic-admin-secret-key")
    monkeypatch.setattr(seaweedfs_bootstrap, "ADMIN_ACCESS_KEY_PATH", access_key_path)
    monkeypatch.setattr(seaweedfs_bootstrap, "ADMIN_SECRET_ACCESS_KEY_PATH", secret_key_path)
    monkeypatch.setenv("INVESTMENT_RESEARCH_S3_ENDPOINT_URL", "http://seaweedfs-s3:8333")
    monkeypatch.setenv("INVESTMENT_RESEARCH_S3_REGION_NAME", "us-east-1")
    monkeypatch.setenv("INVESTMENT_RESEARCH_S3_BUCKET", "investment-research-originals")
    client = FakeBootstrapClient(exists=False)
    captured_configuration: dict[str, object] = {}

    def record_client_configuration(**kwargs: object) -> FakeBootstrapClient:
        captured_configuration.update(kwargs)
        return client

    monkeypatch.setattr(
        seaweedfs_bootstrap,
        "create_s3_client",
        record_client_configuration,
    )

    assert seaweedfs_bootstrap.main() == 0
    assert captured_configuration == {
        "endpoint_url": "http://seaweedfs-s3:8333",
        "region_name": "us-east-1",
        "access_key_id": "synthetic-admin-access-key",
        "secret_access_key": "synthetic-admin-secret-key",
    }
    assert client.create_calls == [{"Bucket": "investment-research-originals"}]
    assert capsys.readouterr().out == "SeaweedFS bucket created.\n"
