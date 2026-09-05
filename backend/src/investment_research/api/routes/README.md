# API Routes

This directory contains thin FastAPI route modules. A route owns HTTP parsing, dependency resolution, status codes, and response serialization; it delegates business behavior to an application service.

## Current routes

| File        | Endpoint      | Purpose                                                             |
| ----------- | ------------- | ------------------------------------------------------------------- |
| `health.py` | `GET /health` | Reports process liveness without dependency details or credentials. |

Product routes for document capture, processing status, research, evidence resolution, and original viewing are added only when their implementation step begins. They must not be advertised as working endpoints before then.

```mermaid
flowchart LR
    REQUEST[HTTP request] --> VALIDATE[FastAPI validation]
    VALIDATE --> CONTEXT[Resolve trusted context]
    CONTEXT --> USECASE[Application use case]
    USECASE --> RESPONSE[Typed HTTP response]
```

Routes must not open another module's tables directly, choose infrastructure adapters, perform long-running parsing, or accept a client-provided workspace as authorization.
