# Operations Infrastructure

This package implements application-owned operational ports without moving observability concerns
into business modules.

## Contents

| File                   | Responsibility                                                                       |
| ---------------------- | ------------------------------------------------------------------------------------ |
| `__init__.py`          | Marks the operational-adapter package.                                               |
| `noop.py`              | Implements tracing without storage, export, credentials, or network.                 |
| `opentelemetry.py`     | Creates W3C trace context and supports optional LangSmith export.                    |

The M0 no-op tracer preserves normal context-manager behavior: application exceptions propagate
unchanged, while trace attributes and safe error categories are ignored. The M1 OpenTelemetry
adapter accepts only application-approved span names, attribute names, UUIDs, technical strings,
and bounded non-negative integers. Automatic exception and traceback recording is disabled.
Tracing setup, attribute, teardown, and provider-shutdown failures degrade to no-op behavior and
never suppress an application exception.

Trace context stays local by default. The local API can opt into LangSmith EU export through
bounded background OTLP/HTTP batches. Exported spans carry allowlisted technical attributes such
as stage durations, model names, OpenAI token counts, and evidence-check counts. The export
excludes application record IDs, source text, prompts, and responses. Export failures leave
request handling independent of LangSmith. [Development setup](../../../../../docs/development.md#optional-local-langsmith-tracing)
