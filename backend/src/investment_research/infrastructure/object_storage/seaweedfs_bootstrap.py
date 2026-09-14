"""Idempotent local SeaweedFS bucket bootstrap entry point."""

from collections.abc import Mapping
from os import environ
from pathlib import Path
from typing import Protocol, cast

from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from investment_research.infrastructure.object_storage.s3 import create_s3_client
from investment_research.settings import ObjectStorageSettings

ADMIN_ACCESS_KEY_PATH = Path("/run/secrets/seaweedfs_admin_access_key")
ADMIN_SECRET_ACCESS_KEY_PATH = Path("/run/secrets/seaweedfs_admin_secret_access_key")


class S3BootstrapClient(Protocol):
    """Narrow administrative S3 surface used only by the one-shot bootstrap."""

    def head_bucket(self, **kwargs: object) -> Mapping[str, object]:
        """Check whether the configured bucket already exists."""

    def create_bucket(self, **kwargs: object) -> Mapping[str, object]:
        """Create the configured private bucket."""


class ObjectStorageBootstrapError(RuntimeError):
    """Report a safe local bootstrap failure without credential details."""


def _read_secret(path: Path, name: str) -> str:
    """Read one mounted Docker secret without including its value in failures."""

    try:
        value = path.read_text(encoding="utf-8").rstrip("\n")
    except OSError as error:
        raise ObjectStorageBootstrapError(f"required {name} secret is unavailable") from error
    if not value:
        raise ObjectStorageBootstrapError(f"required {name} secret is empty")
    return value


def _status_code(error: ClientError) -> int | None:
    """Extract an HTTP status from an untrusted SDK error response."""

    response = cast(Mapping[str, object], error.response)
    metadata = response.get("ResponseMetadata")
    if not isinstance(metadata, Mapping):
        return None
    status = metadata.get("HTTPStatusCode")
    return status if isinstance(status, int) else None


def ensure_bucket(client: S3BootstrapClient, bucket: str) -> bool:
    """Create one missing bucket and return whether creation occurred."""

    try:
        client.head_bucket(Bucket=bucket)
        return False
    except ClientError as error:
        if _status_code(error) != 404:
            raise ObjectStorageBootstrapError("object storage bootstrap failed") from error
    except BotoCoreError as error:
        raise ObjectStorageBootstrapError("object storage bootstrap failed") from error

    try:
        client.create_bucket(Bucket=bucket)
        client.head_bucket(Bucket=bucket)
    except (BotoCoreError, ClientError) as error:
        raise ObjectStorageBootstrapError("object storage bootstrap failed") from error
    return True


def main() -> int:
    """Load process-owned settings and ensure the local canonical bucket exists."""

    admin_access_key = _read_secret(ADMIN_ACCESS_KEY_PATH, "administrator access key")
    admin_secret_access_key = _read_secret(
        ADMIN_SECRET_ACCESS_KEY_PATH,
        "administrator secret key",
    )
    settings = ObjectStorageSettings.model_validate(
        {
            "runtime_location": "container",
            "object_storage_provider": "s3",
            "s3": {
                "endpoint_url": environ.get("INVESTMENT_RESEARCH_S3_ENDPOINT_URL"),
                "region_name": environ.get("INVESTMENT_RESEARCH_S3_REGION_NAME", "us-east-1"),
                "bucket": environ.get("INVESTMENT_RESEARCH_S3_BUCKET"),
                "access_key_id": admin_access_key,
                "secret_access_key": admin_secret_access_key,
            },
        }
    )
    s3 = settings.require_s3_settings()
    client = cast(
        S3BootstrapClient,
        create_s3_client(
            endpoint_url=s3.endpoint_url,
            region_name=s3.region_name,
            access_key_id=s3.access_key_id.get_secret_value(),
            secret_access_key=s3.secret_access_key.get_secret_value(),
        ),
    )
    created = ensure_bucket(client, s3.bucket)
    print("SeaweedFS bucket created." if created else "SeaweedFS bucket already exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
