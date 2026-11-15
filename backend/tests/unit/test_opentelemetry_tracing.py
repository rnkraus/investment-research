"""Tests for content-safe in-process OpenTelemetry tracing."""

import logging
from threading import current_thread
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode, Tracer
from pydantic import SecretStr

from investment_research.infrastructure.operations.opentelemetry import (
    OpenTelemetryOperationTracer,
    create_operation_tracer,
)

RAW_MARKER = "raw question and document text must never appear"


@pytest.mark.parametrize("status_code", [200, 401])
def test_langsmith_export_sends_only_approved_fields(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, status_code: int
) -> None:
    key = "synthetic-export-test-key"
    workspace_id = str(uuid4())
    request_id = str(uuid4())
    calls: list[tuple[str, str, dict[str, str], bytes]] = []
    request_thread = current_thread()

    def capture_request(
        method: str, url: str, *, headers: dict[str, str], data: bytes, **options: object
    ) -> Mock:
        assert current_thread() is not request_thread
        assert options["allow_redirects"] is False
        calls.append((method, url, headers.copy(), data))
        return Mock(status_code=status_code, reason=f"{RAW_MARKER} {key}")

    monkeypatch.setattr("requests.Session.request", Mock(side_effect=capture_request))
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"secret={key}")
    tracer = create_operation_tracer(
        service_name="investment-research-api",
        service_version="1.0.0",
        langsmith_api_key=SecretStr(key),
    )

    try:
        with tracer.start_span(
            "research.answer", attributes={"workspace.id": workspace_id, "question": RAW_MARKER}
        ) as root:
            root.set_attribute("request.id", request_id)
            root.set_attribute("outcome", "accepted")
            with tracer.start_span(
                "research.generate",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": "demo-model",
                    "gen_ai.system": "openai",
                    "llm.model_name": "demo-model",
                },
            ) as generation:
                generation.set_attribute("gen_ai.usage.input_tokens", 80)
                generation.set_attribute("gen_ai.usage.output_tokens", 20)
                generation.set_attribute("gen_ai.usage.total_tokens", 100)
    finally:
        tracer.close()

    [(method, url, headers, body)] = calls
    assert method == "POST"
    assert url == "https://eu.api.smith.langchain.com/otel/v1/traces"
    assert headers["x-api-key"] == key
    assert headers["langsmith-project"] == "investment-research-local"
    for forbidden in (key, workspace_id, request_id, RAW_MARKER):
        assert forbidden.encode() not in body
        assert forbidden not in caplog.text
    if status_code == 401:
        assert any(
            record.levelno == logging.ERROR
            and record.getMessage() == "tracing.langsmith.export_warning"
            for record in caplog.records
        )

    message = ExportTraceServiceRequest.FromString(body)
    [resource_spans] = message.resource_spans
    assert {attribute.key for attribute in resource_spans.resource.attributes} == {
        "service.name",
        "service.version",
    }
    [scope_spans] = resource_spans.scope_spans
    spans = {span.name: span for span in scope_spans.spans}
    assert set(spans) == {"research.answer", "research.generate"}
    assert spans["research.generate"].trace_id == spans["research.answer"].trace_id
    assert spans["research.generate"].parent_span_id == spans["research.answer"].span_id
    assert {attribute.key for attribute in spans["research.answer"].attributes} == {"outcome"}
    attributes = {
        attribute.key: attribute.value for attribute in spans["research.generate"].attributes
    }
    assert attributes["gen_ai.operation.name"].string_value == "chat"
    assert attributes["gen_ai.request.model"].string_value == "demo-model"
    assert attributes["gen_ai.system"].string_value == "openai"
    assert attributes["llm.model_name"].string_value == "demo-model"
    assert attributes["gen_ai.usage.input_tokens"].int_value == 80
    assert attributes["gen_ai.usage.output_tokens"].int_value == 20
    assert attributes["gen_ai.usage.total_tokens"].int_value == 100


def test_langsmith_export_is_disabled_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = Mock()
    monkeypatch.setattr(
        "investment_research.infrastructure.operations.opentelemetry.OTLPSpanExporter", exporter
    )
    tracer = create_operation_tracer(service_name="investment-research-api", service_version="1")
    try:
        with tracer.start_span("research.answer") as span:
            assert span.trace_id is not None
    finally:
        tracer.close()
    exporter.assert_not_called()


def test_langsmith_setup_failure_leaves_tracing_available(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "investment_research.infrastructure.operations.opentelemetry.OTLPSpanExporter",
        Mock(side_effect=ValueError(RAW_MARKER)),
    )
    tracer = create_operation_tracer(
        service_name="investment-research-api",
        service_version="1",
        langsmith_api_key=SecretStr("synthetic-export-test-key"),
    )
    try:
        with tracer.start_span("research.answer") as span:
            assert span.trace_id is not None
    finally:
        tracer.close()
    assert "tracing.langsmith.setup_failed" in caplog.text
    assert RAW_MARKER not in caplog.text


def test_langsmith_does_not_forward_ambient_otlp_headers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "x-untrusted=do-not-send")
    exporter = Mock()
    monkeypatch.setattr(
        "investment_research.infrastructure.operations.opentelemetry.OTLPSpanExporter", exporter
    )
    tracer = create_operation_tracer(
        service_name="investment-research-api",
        service_version="1",
        langsmith_api_key=SecretStr("synthetic-export-test-key"),
    )
    try:
        with tracer.start_span("research.answer") as span:
            assert span.trace_id is not None
    finally:
        tracer.close()
    exporter.assert_not_called()
    assert "tracing.langsmith.ambient_export_settings" in caplog.text


def _capturing_tracer() -> tuple[
    OpenTelemetryOperationTracer,
    InMemorySpanExporter,
    TracerProvider,
]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return (
        OpenTelemetryOperationTracer(provider.get_tracer("test.operations")),
        exporter,
        provider,
    )


def test_nested_spans_share_one_nonzero_trace_id() -> None:
    tracer, exporter, provider = _capturing_tracer()
    workspace_id = uuid4()

    try:
        with (
            tracer.start_span(
                "research.retrieve",
                attributes={
                    "workspace.id": str(workspace_id),
                    "candidate.count": 10,
                },
            ) as outer,
            tracer.start_span("embedding.query") as inner,
        ):
            assert outer.trace_id is not None
            assert len(outer.trace_id) == 32
            assert outer.trace_id != "0" * 32
            assert inner.trace_id == outer.trace_id
    finally:
        provider.shutdown()

    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} == {"research.retrieve", "embedding.query"}
    retrieval_span = next(span for span in spans if span.name == "research.retrieve")
    assert retrieval_span.attributes == {
        "workspace.id": str(workspace_id),
        "candidate.count": 10,
    }


def test_attributes_outside_the_safe_contract_are_not_recorded() -> None:
    tracer, exporter, provider = _capturing_tracer()

    try:
        with tracer.start_span(
            "embedding.query",
            attributes={
                "provider.name": "openai",
                "configuration.version": "embedding-v1",
                "question": RAW_MARKER,
                "model.name": RAW_MARKER,
                "workspace.id": "not-a-uuid",
                "input.units": -1,
            },
        ) as span:
            span.set_attribute("prompt", RAW_MARKER)
            span.set_attribute("outcome", RAW_MARKER)
            span.set_attribute("output.units", 12)
    finally:
        provider.shutdown()

    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    assert finished[0].attributes == {
        "provider.name": "openai",
        "configuration.version": "embedding-v1",
        "output.units": 12,
    }
    assert RAW_MARKER not in repr(finished[0])


def test_safe_error_category_does_not_capture_exception_details() -> None:
    tracer, exporter, provider = _capturing_tracer()

    try:
        with (
            pytest.raises(RuntimeError, match="provider leaked raw content"),
            tracer.start_span("research.generate") as span,
        ):
            span.record_error("provider_timeout")
            raise RuntimeError("provider leaked raw content")
    finally:
        provider.shutdown()

    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    assert finished[0].attributes == {"error.type": "provider_timeout"}
    assert finished[0].status.status_code is StatusCode.ERROR
    assert finished[0].status.description is None
    assert finished[0].events == ()
    assert "provider leaked raw content" not in repr(finished[0])


def test_unknown_span_name_is_a_non_exporting_fallback() -> None:
    tracer, exporter, provider = _capturing_tracer()

    try:
        with tracer.start_span("unapproved.operation") as span:
            assert span.trace_id is None
    finally:
        provider.shutdown()

    assert exporter.get_finished_spans() == ()


def test_reranking_span_records_only_content_free_usage() -> None:
    tracer, exporter, provider = _capturing_tracer()
    try:
        with tracer.start_span(
            "search.rerank",
            attributes={
                "provider.name": "pinecone",
                "model.name": "bge-reranker-v2-m3",
                "operation.kind": "RERANKING",
                "input.units": 1,
                "output.units": 0,
                "question": RAW_MARKER,
            },
        ) as span:
            assert span.trace_id is not None
            span.set_attribute("source_text", RAW_MARKER)
    finally:
        provider.shutdown()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "search.rerank"
    assert spans[0].attributes == {
        "provider.name": "pinecone",
        "model.name": "bge-reranker-v2-m3",
        "operation.kind": "RERANKING",
        "input.units": 1,
        "output.units": 0,
    }
    assert RAW_MARKER not in repr(spans[0])


def test_tracer_start_failure_does_not_fail_product_behavior() -> None:
    class FailingTracer:
        def start_as_current_span(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("tracing unavailable")

    tracer = OpenTelemetryOperationTracer(cast(Tracer, FailingTracer()))

    with tracer.start_span("research.retrieve") as span:
        assert span.trace_id is None


def test_factory_creates_local_trace_context_without_an_exporter() -> None:
    tracer = create_operation_tracer(
        service_name="investment-research-api",
        service_version="0.0.0",
    )

    try:
        with tracer.start_span("document.capture") as span:
            assert span.trace_id is not None
    finally:
        tracer.close()
