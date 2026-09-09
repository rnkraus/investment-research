# Application Package

`investment_research` is the shared modular-monolith package. Entry points assemble dependencies here and invoke typed application boundaries; they do not own business state.

## Contents

| Path              | Responsibility                                                                                                         |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `main.py`         | Creates FastAPI, installs transport safeguards and error handlers, attaches the container, and registers routes.       |
| `bootstrap.py`    | Composition root that selects concrete implementations for application-owned contracts.                                |
| `settings.py`     | Loads process-first local configuration and validates secret-safe runtime settings.                                     |
| `api/`            | HTTP transport models, dependencies, OpenAPI generation, and routes.                                                   |
| `infrastructure/` | Shared technical adapters for SQLAlchemy transactions and secure local object storage.                                  |
| `modules/`        | Business capabilities with explicit ownership and dependency boundaries.                                               |

## Startup flow

```mermaid
flowchart TD
    SETTINGS[Validated settings] --> MAIN[create_app]
    MAIN --> LIFE[FastAPI lifespan]
    LIFE -->|startup| BOOT[build_container]
    BOOT --> ENGINE[SQLAlchemy engine]
    ENGINE --> SESSIONS[Session factory]
    SESSIONS --> UOW[Capture unit-of-work factory]
    BOOT --> STORAGE[ObjectStorage adapter]
    UOW --> CAPTURE[CaptureDocumentService]
    STORAGE --> CAPTURE
    BOOT --> STATE[Application container]
    CAPTURE --> STATE
    MAIN --> LIMIT[Request-body limit]
    LIMIT --> ROUTES[HTTP routes]
    MAIN --> ERRORS[Safe error handlers]
    MAIN --> OPENAPI[OpenAPI contract]
    STATE --> ROUTES
    ROUTES --> ERRORS
    LIFE -->|shutdown| CLOSE[Dispose engine pool]
```

`main.py` and `bootstrap.py` are the outer composition layer. Module domain and application files must remain independent of this layer. Application startup must never create or mutate database schemas.

Importing `create_app` or exporting OpenAPI does not allocate database resources. The FastAPI
lifespan creates them only when the server starts and releases them during shutdown. Sessions are
still created per unit of work; they are never shared across requests or runner executions.

For document capture, the request-body middleware rejects excessive transport payloads before
multipart parsing. The route then resolves the server-owned workspace and capture service from the
application container. The service validates the PDF and coordinates immutable storage with one
database unit of work; the route only translates the typed result or a known safe error.

The package follows the boundaries in [`docs/02-system-design.md`](../../../docs/02-system-design.md#4-component-boundaries).
