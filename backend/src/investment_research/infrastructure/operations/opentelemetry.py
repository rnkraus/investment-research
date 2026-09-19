"""Content-safe in-process OpenTelemetry tracing without an exporter."""

import re
from contextlib import AbstractContextManager, nullcontext, suppress
from types import TracebackType
from typing import Literal, cast
from uuid import UUID

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span, Status, StatusCode, Tracer

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


class OpenTelemetryOperationSpan:
    """Expose a content-safe application span over one OpenTelemetry span."""

    def __init__(self, span: Span) -> None:
        self._span = span

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

    def __init__(self, inner: AbstractContextManager[Span]) -> None:
        self._inner = inner
        self._inner_active = False

    def __enter__(self) -> OperationSpan:
        try:
            span = self._inner.__enter__()
        except Exception:
            return NoopOperationSpan()
        self._inner_active = True
        return OpenTelemetryOperationSpan(span)

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

    def __init__(self, tracer: Tracer, *, provider: TracerProvider | None = None) -> None:
        self._tracer = tracer
        self._provider = provider

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
            inner = self._tracer.start_as_current_span(
                name,
                attributes=_safe_attributes(attributes),
                record_exception=False,
                set_status_on_exception=False,
            )
        except Exception:
            return nullcontext(NoopOperationSpan())
        return _FailOpenSpanContext(cast(AbstractContextManager[Span], inner))

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
) -> OpenTelemetryOperationTracer:
    """Create one process-local tracer provider with deliberately no exporter."""

    if _SAFE_TEXT_PATTERN.fullmatch(service_name) is None:
        raise ValueError("service_name must be a safe technical identifier")
    if _SAFE_TEXT_PATTERN.fullmatch(service_version) is None:
        raise ValueError("service_version must be a safe technical identifier")

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "service.version": service_version,
            }
        )
    )
    tracer = provider.get_tracer("investment_research.operations", service_version)
    return OpenTelemetryOperationTracer(tracer, provider=provider)
