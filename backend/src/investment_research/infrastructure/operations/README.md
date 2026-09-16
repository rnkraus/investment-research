# Operations Infrastructure

This package implements application-owned operational ports without moving observability concerns
into business modules.

## Contents

| File          | Responsibility                                                       |
| ------------- | -------------------------------------------------------------------- |
| `__init__.py` | Marks the operational-adapter package.                               |
| `noop.py`     | Implements tracing without storage, export, credentials, or network. |

The M0 no-op tracer preserves normal context-manager behavior: application exceptions propagate
unchanged, while trace attributes and safe error categories are ignored. Later provider adapters
must remain optional, sanitize exported data, and degrade safely when their backend is unavailable.
