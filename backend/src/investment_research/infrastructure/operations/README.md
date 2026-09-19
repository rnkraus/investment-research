# Operations Infrastructure

This package implements application-owned operational ports without moving observability concerns
into business modules.

## Contents

| File                   | Responsibility                                                                       |
| ---------------------- | ------------------------------------------------------------------------------------ |
| `__init__.py`          | Marks the operational-adapter package.                                               |
| `noop.py`              | Implements tracing without storage, export, credentials, or network.                 |
| `opentelemetry.py`     | Creates content-safe in-process W3C trace context with no configured exporter.       |

The M0 no-op tracer preserves normal context-manager behavior: application exceptions propagate
unchanged, while trace attributes and safe error categories are ignored. The M1 OpenTelemetry
adapter accepts only application-approved span names, attribute names, UUIDs, technical strings,
and bounded non-negative integers. Automatic exception and traceback recording is disabled.
Tracing setup, attribute, teardown, and provider-shutdown failures degrade to no-op behavior and
never suppress an application exception.

The M1 factory deliberately registers no exporter, so trace context remains local to the process.
Later exporters must remain optional, must not expand the content allowlist, and must degrade safely
when their backend is unavailable.
