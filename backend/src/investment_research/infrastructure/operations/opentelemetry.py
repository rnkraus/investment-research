"""Content-safe OpenTelemetry tracing with optional API export."""

import logging
import re
from contextlib import AbstractContextManager, nullcontext, suppress
from os import environ
from types import TracebackType
from typing import Literal, cast
from uuid import UUID

from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from pydantic import SecretStr

from investment_research.infrastructure.logging import application_logger
from investment_research.infrastructure.operations.noop import NoopOperationSpan
from investment_research.modules.operations.ports import (
    APPROVED_OPERATION_SPAN_NAMES,
    APPROVED_TRACE_ATTRIBUTE_NAMES,
    OperationSpan,
    TraceAttributes,
    TraceAttributeValue,
)

_MAX_BOUNDED_INTEGER = 9_223_372_036_854_775_807
_SAFE_TEXT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_LANGSMITH_ENDPOINT = "https://eu.api.smith.langchain.com/otel/v1/traces"
_LANGSMITH_PROJECT = "investment-research-local"
_logger = application_logger("infrastructure.operations.opentelemetry")
_UUID_ATTRIBUTE_NAMES = frozenset(
    {
        "answer.id",
        "correlation.id",
        "job.id",
        "processing.run.id",
        "request.id",
        "research.request.id",
        "workspace.id",
    }
)
_INTEGER_ATTRIBUTE_NAMES = frozenset(
    {
        "candidate.count",
        "chunk.count",
        "cost.micro_usd",
        "duration.ms",
        "input.units",
        "output.units",
    }
)
_TEXT_ATTRIBUTE_NAMES = APPROVED_TRACE_ATTRIBUTE_NAMES - (
    _UUID_ATTRIBUTE_NAMES | _INTEGER_ATTRIBUTE_NAMES
)


def _safe_attribute_value(
    name: str,
    value: TraceAttributeValue,
) -> TraceAttributeValue | None:
    """Return one bounded safe value, or none when it violates the trace contract."""

    if name in _UUID_ATTRIBUTE_NAMES:
        if not isinstance(value, str):
            return None
        try:
            return str(UUID(value))
        except ValueError:
            return None

    if name in _INTEGER_ATTRIBUTE_NAMES:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if not 0 <= value <= _MAX_BOUNDED_INTEGER:
            return None
        return value

    if name in _TEXT_ATTRIBUTE_NAMES:
        if not isinstance(value, str) or _SAFE_TEXT_PATTERN.fullmatch(value) is None:
            return None
        return value

    return None


def _safe_attributes(attributes: TraceAttributes | None) -> dict[str, TraceAttributeValue]:
    """Filter an attribute mapping through the application-owned safe allowlist."""

    if attributes is None:
        return {}
    safe: dict[str, TraceAttributeValue] = {}
    for name, value in attributes.items():
        if name not in APPROVED_TRACE_ATTRIBUTE_NAMES:
            continue
        safe_value = _safe_attribute_value(name, value)
        if safe_value is not None:
            safe[name] = safe_value
    return safe


def _redact_exporter_log(record: logging.LogRecord) -> bool:
    """Keep untrusted HTTP error details out of logs."""

    record.msg = "tracing.langsmith.export_warning"
    record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    return True


class OpenTelemetryOperationSpan:
    """Expose a content-safe application span over one OpenTelemetry span."""

    def __init__(self, span: Span, *, export_enabled: bool = False) -> None:
        self._span = span
        self._export_enabled = export_enabled

    @property
    def trace_id(self) -> str | None:
        """Return the active nonzero W3C trace identifier."""

        span_context = self._span.get_span_context()
        if not span_context.is_valid:
            return None
        return f"{span_context.trace_id:032x}"

    def set_attribute(self, name: str, value: TraceAttributeValue) -> None:
        """Attach one allowlisted and bounded scalar without failing product behavior."""

        if name not in APPROVED_TRACE_ATTRIBUTE_NAMES:
            return
        if self._export_enabled and name in _UUID_ATTRIBUTE_NAMES:
            return
        safe_value = _safe_attribute_value(name, value)
        if safe_value is None:
            return
        try:
            self._span.set_attribute(name, safe_value)
        except Exception:
            return

    def record_error(self, error_type: str) -> None:
        """Record only a safe error category, never exception details or traceback data."""

        safe_error_type = _safe_attribute_value("error.type", error_type)
        if safe_error_type is None:
            return
        try:
            self._span.set_attribute("error.type", safe_error_type)
            self._span.set_status(Status(StatusCode.ERROR))
        except Exception:
            return


class _FailOpenSpanContext(AbstractContextManager[OperationSpan]):
    """Keep tracing setup and teardown failures out of canonical product behavior."""

    def __init__(self, inner: AbstractContextManager[Span], *, export_enabled: bool) -> None:
        self._inner = inner
        self._export_enabled = export_enabled
        self._inner_active = False

    def __enter__(self) -> OperationSpan:
        try:
            span = self._inner.__enter__()
        except Exception:
            return NoopOperationSpan()
        self._inner_active = True
        return OpenTelemetryOperationSpan(span, export_enabled=self._export_enabled)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if self._inner_active:
            with suppress(Exception):
                self._inner.__exit__(exc_type, exc_value, traceback)
        return False


class OpenTelemetryOperationTracer:
    """Create content-safe in-process spans through one process-owned provider."""

    def __init__(
        self,
        tracer: Tracer,
        *,
        provider: TracerProvider | None = None,
        export_enabled: bool = False,
    ) -> None:
        self._tracer = tracer
        self._provider = provider
        self._export_enabled = export_enabled

    def start_span(
        self,
        name: str,
        *,
        attributes: TraceAttributes | None = None,
    ) -> AbstractContextManager[OperationSpan]:
        """Open one approved span while disabling automatic exception capture."""

        if name not in APPROVED_OPERATION_SPAN_NAMES:
            return nullcontext(NoopOperationSpan())
        try:
            safe_attributes = _safe_attributes(attributes)
            if self._export_enabled:
                for attribute_name in _UUID_ATTRIBUTE_NAMES:
                    safe_attributes.pop(attribute_name, None)
            inner = self._tracer.start_as_current_span(
                name,
                attributes=safe_attributes,
                record_exception=False,
                set_status_on_exception=False,
            )
        except Exception:
            return nullcontext(NoopOperationSpan())
        return _FailOpenSpanContext(
            cast(AbstractContextManager[Span], inner), export_enabled=self._export_enabled
        )

    def close(self) -> None:
        """Shut down the owned provider without changing application shutdown behavior."""

        if self._provider is None:
            return
        try:
            self._provider.shutdown()
        except Exception:
            return


def create_operation_tracer(
    *,
    service_name: str,
    service_version: str,
    langsmith_api_key: SecretStr | None = None,
) -> OpenTelemetryOperationTracer:
    """Create one process-local provider with an opt-in, bounded LangSmith exporter."""

    if _SAFE_TEXT_PATTERN.fullmatch(service_name) is None:
        raise ValueError("service_name must be a safe technical identifier")
    if _SAFE_TEXT_PATTERN.fullmatch(service_version) is None:
        raise ValueError("service_version must be a safe technical identifier")

    provider = TracerProvider(
        resource=Resource(
            {
                "service.name": service_name,
                "service.version": service_version,
            }
        ),
    )
    export_enabled = False
    if langsmith_api_key is not None and any(
        name.startswith(("OTEL_EXPORTER_OTLP_", "OTEL_PYTHON_EXPORTER_OTLP_HTTP_"))
        for name in environ
    ):
        # The SDK merges ambient headers even when explicitly configured headers are supplied.
        _logger.warning("tracing.langsmith.ambient_export_settings")
    elif langsmith_api_key is not None:
        try:
            logging.getLogger(OTLPSpanExporter.__module__).addFilter(_redact_exporter_log)
            exporter: SpanExporter = OTLPSpanExporter(
                endpoint=_LANGSMITH_ENDPOINT,
                headers={
                    "x-api-key": langsmith_api_key.get_secret_value(),
                    "Langsmith-Project": _LANGSMITH_PROJECT,
                },
                compression=Compression.NoCompression,
                timeout=2,
                max_request_size=262_144,
            )
            try:
                provider.add_span_processor(
                    BatchSpanProcessor(
                        exporter,
                        max_queue_size=256,
                        max_export_batch_size=32,
                        schedule_delay_millis=1_000,
                        export_timeout_millis=2_000,
                    )
                )
            except Exception:
                exporter.shutdown()
                raise
            export_enabled = True
        except Exception:
            _logger.warning("tracing.langsmith.setup_failed")
    tracer = provider.get_tracer("investment_research.operations", service_version)
    return OpenTelemetryOperationTracer(tracer, provider=provider, export_enabled=export_enabled)
