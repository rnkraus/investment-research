"""Unit tests for application resource wiring and lifecycle ownership."""

from collections.abc import Mapping
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from investment_research import bootstrap as bootstrap_module
from investment_research.bootstrap import (
    ApiContainer,
    build_api_container,
    build_worker_container,
)
from investment_research.infrastructure.health import (
    CompositeReadinessProbe,
    PostgreSQLReadinessProbe,
)
from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.infrastructure.object_storage.s3 import (
    S3ObjectStorage,
    S3ObjectStorageReadinessProbe,
)
from investment_research.infrastructure.queue.celery import (
    CeleryBrokerReadinessProbe,
    CeleryProcessingDispatcher,
)
from investment_research.main import create_app
from investment_research.modules.documents.application import (
    CaptureDocumentService,
    OpenOriginalService,
)
from investment_research.modules.evidence.application import ResolveEvidenceSpanService
from investment_research.modules.processing.application import (
    M1_PROCESSING_PROFILE,
    ExecuteProcessingJobService,
    GetProcessingRunService,
    ProcessDocumentService,
)
from investment_research.modules.research.application import (
    AnswerResearchRequestService,
    GenerateValidatedAnswerService,
    RetrieveResearchService,
)
from investment_research.settings import ApiSettings, WorkerSettings

LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"
LOCAL_REDIS_URL = "redis://:synthetic-secret@127.0.0.1:6379/0"


class UnusedS3Client:
    """Satisfy the narrow client protocol without allowing accidental network-like calls."""

    def head_bucket(self, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError("container construction must not probe S3")

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError("container construction must not write to S3")

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError("container construction must not read from S3")

    def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError("container construction must not delete from S3")


def make_api_settings(object_storage_root: Path) -> ApiSettings:
    """Build deterministic API settings without opening a database connection."""

    return ApiSettings.model_validate(
        {
            "environment": "test",
            "database_url": LOCAL_DATABASE_URL,
            "redis_url": LOCAL_REDIS_URL,
            "object_storage_root": object_storage_root,
        }
    )


def make_worker_settings(object_storage_root: Path) -> WorkerSettings:
    """Build deterministic worker settings without opening a database connection."""

    return WorkerSettings.model_validate(
        {
            "environment": "test",
            "database_url": LOCAL_DATABASE_URL,
            "object_storage_root": object_storage_root,
            "parser_endpoint_url": "http://127.0.0.1:8100",
        }
    )


def s3_provider_values() -> dict[str, object]:
    """Build complete synthetic S3 values shared by API and worker tests."""

    return {
        "environment": "test",
        "database_url": LOCAL_DATABASE_URL,
        "redis_url": LOCAL_REDIS_URL,
        "object_storage_provider": "s3",
        "s3": {
            "endpoint_url": "http://127.0.0.1:8333",
            "region_name": "us-east-1",
            "bucket": "investment-research-originals",
            "access_key_id": "synthetic-application-key",
            "secret_access_key": "synthetic-application-secret",
        },
    }


def test_build_api_container_wires_only_api_resources(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    settings = make_api_settings(storage_root)
    container = build_api_container(settings)

    try:
        session = container.session_factory()
        try:
            assert container.settings is settings
            assert container.database_engine.dialect.name == "postgresql"
            assert session.bind is container.database_engine
            assert isinstance(container.object_storage, LocalObjectStorage)
            assert isinstance(
                container.processing_dispatcher,
                CeleryProcessingDispatcher,
            )
            assert container.queue_application.conf.broker_url == LOCAL_REDIS_URL
            assert container.queue_application.conf.result_backend is None
            assert isinstance(container.readiness_probe, CompositeReadinessProbe)
            assert tuple(type(probe) for probe in container.readiness_probe.probes) == (
                PostgreSQLReadinessProbe,
                CeleryBrokerReadinessProbe,
            )
            assert isinstance(container.capture_document_service, CaptureDocumentService)
            assert (
                container.capture_document_service._processing_profile.lexical_config_version
                == M1_PROCESSING_PROFILE.lexical_config_version
            )
            assert isinstance(container.open_original_service, OpenOriginalService)
            assert isinstance(container.get_processing_run_service, GetProcessingRunService)
            assert isinstance(container.retrieve_research_service, RetrieveResearchService)
            assert (
                container.retrieve_research_service._search_profile.lexical_config_version
                == M1_PROCESSING_PROFILE.lexical_config_version
            )
            assert isinstance(
                container.generate_validated_answer_service,
                GenerateValidatedAnswerService,
            )
            assert isinstance(
                container.answer_research_request_service,
                AnswerResearchRequestService,
            )
            assert isinstance(
                container.resolve_evidence_span_service,
                ResolveEvidenceSpanService,
            )
            assert not hasattr(container, "process_document_service")
            assert not storage_root.exists()
        finally:
            session.close()
    finally:
        container.close()


def test_build_worker_container_wires_only_processing_resources(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    settings = make_worker_settings(storage_root)
    container = build_worker_container(settings)

    try:
        session = container.session_factory()
        try:
            assert container.settings is settings
            assert container.database_engine.dialect.name == "postgresql"
            assert session.bind is container.database_engine
            assert isinstance(container.object_storage, LocalObjectStorage)
            assert isinstance(container.open_original_service, OpenOriginalService)
            assert isinstance(container.process_document_service, ProcessDocumentService)
            assert isinstance(
                container.execute_processing_job_service,
                ExecuteProcessingJobService,
            )
            assert not hasattr(container, "capture_document_service")
            assert not hasattr(container, "answer_research_request_service")
            assert not storage_root.exists()
        finally:
            session.close()
    finally:
        container.close()


def test_s3_provider_wires_same_adapter_into_api_and_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = UnusedS3Client()
    client_calls: list[dict[str, object]] = []

    def record_client_configuration(**kwargs: object) -> UnusedS3Client:
        client_calls.append(kwargs)
        return client

    monkeypatch.setattr(bootstrap_module, "create_s3_client", record_client_configuration)
    api_container = build_api_container(ApiSettings.model_validate(s3_provider_values()))
    worker_container = build_worker_container(
        WorkerSettings.model_validate(
            {
                **s3_provider_values(),
                "parser_endpoint_url": "http://127.0.0.1:8100",
            }
        )
    )

    try:
        assert isinstance(api_container.object_storage, S3ObjectStorage)
        assert isinstance(worker_container.object_storage, S3ObjectStorage)
        assert isinstance(api_container.readiness_probe, CompositeReadinessProbe)
        assert tuple(type(probe) for probe in api_container.readiness_probe.probes) == (
            PostgreSQLReadinessProbe,
            CeleryBrokerReadinessProbe,
            S3ObjectStorageReadinessProbe,
        )
        assert client_calls == [
            {
                "endpoint_url": "http://127.0.0.1:8333",
                "region_name": "us-east-1",
                "access_key_id": "synthetic-application-key",
                "secret_access_key": "synthetic-application-secret",
            },
            {
                "endpoint_url": "http://127.0.0.1:8333",
                "region_name": "us-east-1",
                "access_key_id": "synthetic-application-key",
                "secret_access_key": "synthetic-application-secret",
            },
        ]
    finally:
        api_container.close()
        worker_container.close()


def test_application_lifespan_owns_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    closed_containers: list[ApiContainer] = []

    def record_close(container: ApiContainer) -> None:
        closed_containers.append(container)

    monkeypatch.setattr(ApiContainer, "close", record_close)
    application = create_app(make_api_settings(tmp_path / "objects"))

    assert not hasattr(application.state, "container")

    with TestClient(application):
        container = application.state.container
        assert isinstance(container, ApiContainer)

    assert closed_containers == [container]
    assert not hasattr(application.state, "container")
