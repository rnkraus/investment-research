"""Unit tests for application resource wiring and lifecycle ownership."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, create_autospec

import pytest
from celery import Celery  # type: ignore[import-untyped]
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from investment_research import bootstrap as bootstrap_module
from investment_research.bootstrap import (
    ApiContainer,
    build_api_container,
    build_worker_container,
)
from investment_research.infrastructure.evidence.openai import (
    OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
    OPENAI_STATEMENT_SUPPORT_MODEL,
    OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS,
    OpenAIStatementSupportEvaluator,
)
from investment_research.infrastructure.generation.openai import (
    OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
    OPENAI_ANSWER_MODEL,
    OPENAI_ANSWER_TIMEOUT_SECONDS,
    OpenAIAnswerGenerator,
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
from investment_research.infrastructure.search.openai import OpenAIEmbeddingProvider
from investment_research.infrastructure.search.pinecone import (
    PineconeDenseIndexWriter,
    PineconeDenseRetriever,
)
from investment_research.main import create_app
from investment_research.modules.documents.application import (
    CaptureDocumentService,
    OpenOriginalService,
)
from investment_research.modules.evidence.application import ResolveEvidenceSpanService
from investment_research.modules.processing.application import (
    M0_0_PROCESSING_PROFILE,
    M1_PROCESSING_PROFILE,
    ExecuteProcessingJobService,
    GetProcessingRunService,
    ProcessDocumentService,
)
from investment_research.modules.processing.indexing import IndexProcessingRunService
from investment_research.modules.research.application import (
    AnswerResearchRequestService,
    GenerateValidatedAnswerService,
    RetrieveResearchService,
)
from investment_research.modules.search.contracts import M1_LANE_CANDIDATE_LIMIT
from investment_research.modules.search.indexing import IndexActiveChunksService
from investment_research.settings import ApiSettings, WorkerSettings

LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"
LOCAL_REDIS_URL = "redis://:synthetic-secret@127.0.0.1:6379/0"


def indexing_provider_values() -> dict[str, object]:
    """Build complete synthetic M1 provider settings without making provider calls."""

    return {
        "embedding": {"api_key": "synthetic-openai-key"},
        "pinecone": {
            "api_key": "synthetic-pinecone-key",
            "index_host": "synthetic-index.example.svc.pinecone.io",
        },
    }


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
            "provider_monthly_budget_micro_usd": 5_000_000,
            "indexing_providers": indexing_provider_values(),
        }
    )


def make_m1_api_settings(object_storage_root: Path) -> ApiSettings:
    """Build API settings with complete synthetic query-provider credentials."""

    return ApiSettings.model_validate(
        {
            "environment": "test",
            "database_url": LOCAL_DATABASE_URL,
            "redis_url": LOCAL_REDIS_URL,
            "object_storage_root": object_storage_root,
            "provider_monthly_budget_micro_usd": 5_000_000,
            "query_providers": indexing_provider_values(),
            "generation_provider": {"api_key": "synthetic-generation-key"},
        }
    )


@dataclass(frozen=True, slots=True)
class ApiProviderFakes:
    """Lifecycle-visible doubles for the complete M1 API provider group."""

    embedding_provider: Mock
    dense_retriever: Mock
    answer_generator: Mock
    support_evaluator: Mock
    embedding_factory: Mock
    dense_factory: Mock
    answer_factory: Mock
    support_factory: Mock


def install_api_provider_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> ApiProviderFakes:
    """Replace provider factories with lifecycle-visible, network-free test doubles."""

    embedding_provider = create_autospec(OpenAIEmbeddingProvider, instance=True)
    dense_retriever = create_autospec(PineconeDenseRetriever, instance=True)
    answer_generator = create_autospec(OpenAIAnswerGenerator, instance=True)
    support_evaluator = create_autospec(OpenAIStatementSupportEvaluator, instance=True)
    embedding_factory = Mock(return_value=embedding_provider)
    dense_factory = Mock(return_value=dense_retriever)
    answer_factory = Mock(return_value=answer_generator)
    support_factory = Mock(return_value=support_evaluator)
    monkeypatch.setattr(
        bootstrap_module,
        "create_openai_embedding_provider",
        embedding_factory,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "create_pinecone_dense_retriever",
        dense_factory,
    )
    monkeypatch.setattr(bootstrap_module, "create_openai_answer_generator", answer_factory)
    monkeypatch.setattr(
        bootstrap_module,
        "create_openai_statement_support_evaluator",
        support_factory,
    )
    return ApiProviderFakes(
        embedding_provider=embedding_provider,
        dense_retriever=dense_retriever,
        answer_generator=answer_generator,
        support_evaluator=support_evaluator,
        embedding_factory=embedding_factory,
        dense_factory=dense_factory,
        answer_factory=answer_factory,
        support_factory=support_factory,
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
            assert container.query_embedding_provider is None
            assert container.dense_retriever is None
            assert container.answer_generator_provider is None
            assert container.statement_support_provider is None
            assert container.provider_operation_executor is None
            assert container.retrieve_research_service._m1_query_retrieval is None
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


def test_build_api_container_wires_m1_query_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fakes = install_api_provider_fakes(monkeypatch)
    settings = make_m1_api_settings(tmp_path / "objects")
    provider_settings = settings.require_query_providers()
    container = build_api_container(settings)

    try:
        dependencies = container.retrieve_research_service._m1_query_retrieval
        assert container.query_embedding_provider is fakes.embedding_provider
        assert container.dense_retriever is fakes.dense_retriever
        assert container.answer_generator_provider is fakes.answer_generator
        assert container.statement_support_provider is fakes.support_evaluator
        assert container.provider_operation_executor is not None
        assert (
            container.generate_validated_answer_service._answer_generator is fakes.answer_generator
        )
        assert (
            container.generate_validated_answer_service._support_evaluator
            is fakes.support_evaluator
        )
        assert dependencies is not None
        assert dependencies.embedding_provider is container.query_embedding_provider
        assert dependencies.dense_retriever is container.dense_retriever
        assert dependencies.provider_operation_executor is container.provider_operation_executor
        assert (
            dependencies.provider_operation_executor.reservation_service.budget_policy.monthly_limit_micro_usd
            == 5_000_000
        )
        assert (
            container.retrieve_research_service._search_profile.candidate_limit
            == M1_LANE_CANDIDATE_LIMIT
        )
        assert dependencies.dense_profile.candidate_limit == M1_LANE_CANDIDATE_LIMIT
        assert (
            dependencies.dense_profile.embedding_config_version
            == M1_PROCESSING_PROFILE.embedding_config_version
        )
        assert (
            dependencies.dense_profile.dense_index_config_version
            == M1_PROCESSING_PROFILE.dense_index_config_version
        )
        fakes.embedding_factory.assert_called_once_with(
            api_key="synthetic-openai-key",
            embedding_config_version=provider_settings.embedding.config_version,
            model=provider_settings.embedding.model,
            dimensions=provider_settings.embedding.dimensions,
            operation_timeout_seconds=provider_settings.embedding.timeout_seconds,
        )
        fakes.dense_factory.assert_called_once_with(
            api_key="synthetic-pinecone-key",
            index_host=provider_settings.pinecone.index_host,
            operation_timeout_seconds=provider_settings.pinecone.operation_timeout_seconds,
        )
        fakes.answer_factory.assert_called_once_with(
            api_key="synthetic-generation-key",
            model=OPENAI_ANSWER_MODEL,
            operation_timeout_seconds=OPENAI_ANSWER_TIMEOUT_SECONDS,
            max_output_tokens=OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
        )
        fakes.support_factory.assert_called_once_with(
            api_key="synthetic-generation-key",
            model=OPENAI_STATEMENT_SUPPORT_MODEL,
            operation_timeout_seconds=OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS,
            max_output_tokens=OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
        )
    finally:
        container.close()
    fakes.support_evaluator.close.assert_called_once_with()
    fakes.answer_generator.close.assert_called_once_with()
    fakes.dense_retriever.close.assert_called_once_with()
    fakes.embedding_provider.close.assert_called_once_with()


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
            assert isinstance(container.embedding_provider, OpenAIEmbeddingProvider)
            assert isinstance(container.dense_index_writer, PineconeDenseIndexWriter)
            assert isinstance(container.open_original_service, OpenOriginalService)
            assert isinstance(container.process_document_service, ProcessDocumentService)
            assert isinstance(
                container.index_active_chunks_service,
                IndexActiveChunksService,
            )
            assert isinstance(
                container.index_processing_run_service,
                IndexProcessingRunService,
            )
            assert isinstance(
                container.execute_processing_job_service,
                ExecuteProcessingJobService,
            )
            assert (
                container.index_active_chunks_service.embedding_provider
                is container.embedding_provider
            )
            assert (
                container.index_active_chunks_service.dense_index_writer
                is container.dense_index_writer
            )
            assert (
                container.index_processing_run_service._indexing_service
                is container.index_active_chunks_service
            )
            assert (
                container.execute_processing_job_service.indexing_service
                is container.index_processing_run_service
            )
            assert not hasattr(container, "capture_document_service")
            assert not hasattr(container, "answer_research_request_service")
            assert not storage_root.exists()
        finally:
            session.close()
    finally:
        container.close()


def test_build_worker_container_supports_credential_free_m0_regression(
    tmp_path: Path,
) -> None:
    settings = WorkerSettings.model_validate(
        {
            "environment": "test",
            "database_url": LOCAL_DATABASE_URL,
            "object_storage_root": tmp_path / "objects",
            "parser_endpoint_url": "http://127.0.0.1:8100",
            "processing_profile": "m0-regression",
        }
    )
    container = build_worker_container(settings)

    try:
        assert container.embedding_provider is None
        assert container.dense_index_writer is None
        assert container.index_active_chunks_service is None
        assert container.index_processing_run_service is None
        assert container.execute_processing_job_service.indexing_service is None
        assert container.process_document_service._profile is M0_0_PROCESSING_PROFILE
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
                "provider_monthly_budget_micro_usd": 5_000_000,
                "indexing_providers": indexing_provider_values(),
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


def test_worker_container_closes_provider_and_database_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    original_dense_close = PineconeDenseIndexWriter.close
    original_embedding_close = OpenAIEmbeddingProvider.close
    original_database_dispose = Engine.dispose

    def close_dense(writer: PineconeDenseIndexWriter) -> None:
        events.append("dense-index")
        original_dense_close(writer)

    def close_embedding(provider: OpenAIEmbeddingProvider) -> None:
        events.append("embedding")
        original_embedding_close(provider)

    def dispose_database(engine: Engine) -> None:
        events.append("database")
        original_database_dispose(engine)

    monkeypatch.setattr(PineconeDenseIndexWriter, "close", close_dense)
    monkeypatch.setattr(OpenAIEmbeddingProvider, "close", close_embedding)
    monkeypatch.setattr(Engine, "dispose", dispose_database)
    container = build_worker_container(make_worker_settings(tmp_path / "objects"))

    container.close()

    assert events == ["dense-index", "embedding", "database"]


def test_api_container_closes_query_provider_and_database_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    fakes = install_api_provider_fakes(monkeypatch)
    original_database_dispose = Engine.dispose

    def dispose_database(engine: Engine) -> None:
        events.append("database")
        original_database_dispose(engine)

    fakes.support_evaluator.close.side_effect = lambda: events.append("statement-support")
    fakes.answer_generator.close.side_effect = lambda: events.append("answer-generation")
    fakes.dense_retriever.close.side_effect = lambda: events.append("dense-query")
    fakes.embedding_provider.close.side_effect = lambda: events.append("query-embedding")
    monkeypatch.setattr(Engine, "dispose", dispose_database)
    container = build_api_container(make_m1_api_settings(tmp_path / "objects"))

    container.close()

    assert events == [
        "statement-support",
        "answer-generation",
        "dense-query",
        "query-embedding",
        "database",
    ]


def test_api_container_releases_owned_resources_when_query_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    fakes = install_api_provider_fakes(monkeypatch)
    fakes.dense_factory.side_effect = RuntimeError("simulated dense query setup failure")
    original_queue_close = Celery.close
    original_database_dispose = Engine.dispose

    fakes.embedding_provider.close.side_effect = lambda: events.append("query-embedding")

    def close_queue(application: Celery) -> None:
        events.append("queue")
        original_queue_close(application)

    def dispose_database(engine: Engine) -> None:
        events.append("database")
        original_database_dispose(engine)

    monkeypatch.setattr(Celery, "close", close_queue)
    monkeypatch.setattr(Engine, "dispose", dispose_database)

    with pytest.raises(RuntimeError, match="simulated dense query setup failure"):
        build_api_container(make_m1_api_settings(tmp_path / "objects"))

    assert events == ["query-embedding", "queue", "database"]


def test_api_container_releases_all_prior_resources_when_support_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    fakes = install_api_provider_fakes(monkeypatch)
    fakes.support_factory.side_effect = RuntimeError("simulated support setup failure")
    original_queue_close = Celery.close
    original_database_dispose = Engine.dispose

    fakes.answer_generator.close.side_effect = lambda: events.append("answer-generation")
    fakes.dense_retriever.close.side_effect = lambda: events.append("dense-query")
    fakes.embedding_provider.close.side_effect = lambda: events.append("query-embedding")

    def close_queue(application: Celery) -> None:
        events.append("queue")
        original_queue_close(application)

    def dispose_database(engine: Engine) -> None:
        events.append("database")
        original_database_dispose(engine)

    monkeypatch.setattr(Celery, "close", close_queue)
    monkeypatch.setattr(Engine, "dispose", dispose_database)

    with pytest.raises(RuntimeError, match="simulated support setup failure"):
        build_api_container(make_m1_api_settings(tmp_path / "objects"))

    assert events == [
        "answer-generation",
        "dense-query",
        "query-embedding",
        "queue",
        "database",
    ]


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
