"""Tests for content-safe in-process OpenTelemetry tracing."""

from typing import cast
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode, Tracer

from investment_research.infrastructure.operations.opentelemetry import (
    OpenTelemetryOperationTracer,
    create_operation_tracer,
)

RAW_MARKER = "raw question and document text must never appear"


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
