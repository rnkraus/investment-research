"""Opt-in contract tests against an isolated SeaweedFS S3 data plane."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from threading import Barrier
from typing import Protocol, cast
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from fastapi.testclient import TestClient

from investment_research.api.contracts import DocumentAccepted
from investment_research.infrastructure.object_storage.s3 import (
    S3Client,
    S3ObjectStorage,
    create_s3_client,
)
from investment_research.main import create_app
from investment_research.modules.documents.ports import (
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectStorageScopeError,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.settings import load_api_settings

INTEGRATION_FLAG = "INVESTMENT_RESEARCH_OBJECT_STORAGE_INTEGRATION"
PERSISTENCE_PHASE = "INVESTMENT_RESEARCH_OBJECT_STORAGE_PERSISTENCE_PHASE"
USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
OTHER_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
PERSISTENCE_OBJECT_ID = UUID("00000000-0000-4000-8000-000000000004")
PDF_CONTENT = b"%PDF-1.7\nsynthetic SeaweedFS integration document\n%%EOF\n"
OTHER_PDF_CONTENT = b"%PDF-1.7\ncompeting immutable document\n%%EOF\n"


class AdministrativeS3Client(Protocol):
    """Administrative calls that application credentials must not be allowed to use."""

    def create_bucket(self, **kwargs: object) -> object:
        """Attempt to create a bucket."""


@dataclass(frozen=True)
class S3IntegrationConfiguration:
    """Explicit disposable S3 values supplied by the verification script."""

    endpoint_url: str
    region_name: str
    bucket: str
    access_key_id: str
    secret_access_key: str

    def create_client(self) -> S3Client:
        """Create one independently pooled client for a test operation."""

        return create_s3_client(
            endpoint_url=self.endpoint_url,
            region_name=self.region_name,
            access_key_id=self.access_key_id,
            secret_access_key=self.secret_access_key,
        )

    def create_storage(self) -> S3ObjectStorage:
        """Create the application adapter for the disposable bucket."""

        return S3ObjectStorage(client=self.create_client(), bucket=self.bucket)


def required_environment_value(name: str) -> str:
    """Load one script-owned value without falling back to repository environment files."""

    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must be supplied by the isolated verification script")
    return value


@pytest.fixture(scope="module")
def s3_configuration() -> S3IntegrationConfiguration:
    """Require an explicit opt-in before any real S3-compatible request is made."""

    if os.environ.get(INTEGRATION_FLAG) != "1":
        pytest.skip("isolated object-storage verification is not active")
    return S3IntegrationConfiguration(
        endpoint_url=required_environment_value("INVESTMENT_RESEARCH_S3_ENDPOINT_URL"),
        region_name=required_environment_value("INVESTMENT_RESEARCH_S3_REGION_NAME"),
        bucket=required_environment_value("INVESTMENT_RESEARCH_S3_BUCKET"),
        access_key_id=required_environment_value("INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID"),
        secret_access_key=required_environment_value("INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY"),
    )


def make_context(workspace_id: UUID = WORKSPACE_ID) -> WorkspaceContext:
    """Build one trusted workspace context for an integration operation."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=workspace_id)


def original_key(object_id: UUID, workspace_id: UUID = WORKSPACE_ID) -> str:
    """Return the canonical server-owned key shape for one original."""

    return f"workspaces/{workspace_id}/originals/{object_id}.pdf"


def test_round_trip_metadata_and_idempotent_discard(
    s3_configuration: S3IntegrationConfiguration,
) -> None:
    storage = s3_configuration.create_storage()
    key = original_key(uuid4())

    stored = storage.store_immutable(make_context(), key, BytesIO(PDF_CONTENT))
    with storage.open(make_context(), key) as body:
        restored = body.read()

    assert restored == PDF_CONTENT
    assert stored.byte_size == len(PDF_CONTENT)
    assert len(stored.checksum_sha256) == 64

    storage.discard_uncommitted(make_context(), key)
    storage.discard_uncommitted(make_context(), key)
    with (
        pytest.raises(ObjectNotFoundError, match="object is unavailable"),
        storage.open(make_context(), key),
    ):
        pytest.fail("a discarded object must not remain readable")


def test_concurrent_conditional_writes_create_exactly_one_object(
    s3_configuration: S3IntegrationConfiguration,
) -> None:
    key = original_key(uuid4())
    barrier = Barrier(2)

    def write(content: bytes) -> tuple[str, bytes]:
        storage = s3_configuration.create_storage()
        barrier.wait()
        try:
            storage.store_immutable(make_context(), key, BytesIO(content))
        except ObjectAlreadyExistsError:
            return ("conflict", content)
        return ("stored", content)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, (PDF_CONTENT, OTHER_PDF_CONTENT)))

    assert sorted(result for result, _ in outcomes) == ["conflict", "stored"]
    winning_content = next(content for result, content in outcomes if result == "stored")
    storage = s3_configuration.create_storage()
    try:
        with storage.open(make_context(), key) as body:
            assert body.read() == winning_content
    finally:
        storage.discard_uncommitted(make_context(), key)


def test_missing_and_foreign_reads_are_indistinguishable(
    s3_configuration: S3IntegrationConfiguration,
) -> None:
    storage = s3_configuration.create_storage()
    object_id = uuid4()
    messages: list[str] = []

    for key in (original_key(object_id), original_key(object_id, OTHER_WORKSPACE_ID)):
        with (
            pytest.raises(ObjectNotFoundError) as error,
            storage.open(make_context(), key),
        ):
            pytest.fail("an unavailable object must never be opened")
        messages.append(str(error.value))

    assert messages == ["object is unavailable", "object is unavailable"]
    with pytest.raises(ObjectStorageScopeError, match="outside the active workspace"):
        storage.store_immutable(
            make_context(),
            original_key(object_id, OTHER_WORKSPACE_ID),
            BytesIO(PDF_CONTENT),
        )


def test_application_credentials_cannot_create_buckets(
    s3_configuration: S3IntegrationConfiguration,
) -> None:
    client = cast(AdministrativeS3Client, s3_configuration.create_client())

    with pytest.raises(ClientError) as error:
        client.create_bucket(Bucket=f"unauthorized-{uuid4().hex}")

    assert error.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403


def test_api_capture_and_authorized_original_open_use_seaweedfs(
    s3_configuration: S3IntegrationConfiguration,
) -> None:
    settings = load_api_settings(env_file=None)
    assert settings.require_s3_settings().bucket == s3_configuration.bucket
    application = create_app(settings)

    with TestClient(application) as client:
        capture_response = client.post(
            "/api/v1/documents",
            data={
                "title": "Synthetic SeaweedFS API Integration",
                "language": "en",
                "published_at": "2026-02-19T00:00:00Z",
            },
            files={"file": ("integration.pdf", BytesIO(PDF_CONTENT), "application/pdf")},
        )

        assert capture_response.status_code == 202
        capture = DocumentAccepted.model_validate(capture_response.json())
        original_response = client.get(
            f"/api/v1/documents/{capture.document_id}/versions/"
            f"{capture.document_version_id}/original"
        )

    assert original_response.status_code == 200
    assert original_response.headers["content-type"] == "application/pdf"
    assert original_response.content == PDF_CONTENT


def test_object_survives_storage_restart(
    s3_configuration: S3IntegrationConfiguration,
) -> None:
    phase = os.environ.get(PERSISTENCE_PHASE)
    if phase not in {"write", "read"}:
        pytest.skip("restart-persistence phase is controlled by the verification script")

    storage = s3_configuration.create_storage()
    key = original_key(PERSISTENCE_OBJECT_ID)
    if phase == "write":
        storage.store_immutable(make_context(), key, BytesIO(PDF_CONTENT))
        return

    try:
        with storage.open(make_context(), key) as body:
            assert body.read() == PDF_CONTENT
    finally:
        storage.discard_uncommitted(make_context(), key)
