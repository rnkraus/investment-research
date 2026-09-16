"""Application-owned contracts for optional operational tracing."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Protocol

type TraceAttributeValue = str | bool | int | float
type TraceAttributes = Mapping[str, TraceAttributeValue]


class OperationSpan(Protocol):
    """Record bounded metadata for one application operation."""

    def set_attribute(self, name: str, value: TraceAttributeValue) -> None:
        """Attach one safe scalar attribute without raw document or prompt content."""

    def record_error(self, error_type: str) -> None:
        """Record one safe error category without exception text or traceback data."""


class OperationTracer(Protocol):
    """Open optional spans without coupling application code to a tracing SDK."""

    def start_span(
        self,
        name: str,
        *,
        attributes: TraceAttributes | None = None,
    ) -> AbstractContextManager[OperationSpan]:
        """Return a context manager for one named application operation."""
