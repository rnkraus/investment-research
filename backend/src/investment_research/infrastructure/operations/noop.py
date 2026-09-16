"""No-op tracing adapter for runtimes without an observability provider."""

from contextlib import AbstractContextManager, nullcontext

from investment_research.modules.operations.ports import (
    OperationSpan,
    TraceAttributes,
    TraceAttributeValue,
)


class NoopOperationSpan:
    """Accept span events without storing or exporting them."""

    def set_attribute(self, name: str, value: TraceAttributeValue) -> None:
        """Ignore one safe scalar attribute."""

        del name, value

    def record_error(self, error_type: str) -> None:
        """Ignore one safe error category while preserving exception propagation."""

        del error_type


class NoopOperationTracer:
    """Open in-process no-op spans without network or provider dependencies."""

    def start_span(
        self,
        name: str,
        *,
        attributes: TraceAttributes | None = None,
    ) -> AbstractContextManager[OperationSpan]:
        """Return a context manager that performs no tracing work."""

        del name, attributes
        span: OperationSpan = NoopOperationSpan()
        return nullcontext(span)
