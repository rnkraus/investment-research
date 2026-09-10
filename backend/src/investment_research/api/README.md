# API Boundary

This directory owns HTTP-facing concerns. It translates between FastAPI requests and typed application operations without becoming the owner of business rules or persistence.

## Contents

| Path              | Responsibility                                                                       |
| ----------------- | ------------------------------------------------------------------------------------ |
| `contracts.py`    | Strict request, result, error, evidence, and versioned stream-event schemas.         |
| `dependencies.py` | Exposes application services and the server-resolved workspace context to routes.   |
| `errors.py`       | Serializes expected failures and validation errors without exposing private input.  |
| `middleware.py`   | Rejects oversized or ambiguously framed request bodies before multipart parsing.    |
| `openapi.py`      | Registers public Pydantic contracts in the generated OpenAPI document.               |
| `routes/`         | Thin HTTP endpoint modules.                                                          |

## Request boundary

```mermaid
flowchart LR
    CLIENT[Frontend client] --> LIMIT[Request-body limit]
    LIMIT --> ROUTE[FastAPI route]
    ROUTE --> DEP[Server-owned dependencies]
    DEP --> CONTEXT[Workspace context]
    ROUTE --> SERVICE[Typed application service]
    SERVICE --> RESULT[Typed result]
    RESULT --> ROUTE
    ROUTE --> ERROR[Safe error translation]
    ROUTE --> CLIENT
    ERROR --> CLIENT
```

Client-supplied identifiers are untrusted. In particular, `workspace_id` is not accepted as authorization proof; routes obtain workspace scope through `WorkspaceContextDependency`. Error responses expose stable codes and safe summaries rather than internal details.

The processing-status route accepts only a processing-run UUID and delegates to
`GetProcessingRunService`. That service opens a fresh read session and applies the same trusted
workspace boundary as the runner's repository operations. The API never starts parsing, shares a
runner session, or exposes processing configuration and exception details. Unknown and foreign run
identifiers are intentionally indistinguishable.

The transport-wide body limit includes a small allowance for multipart framing and rejects excessive
declared or streamed bytes before endpoint execution. The document capture service independently
enforces the configured PDF-byte limit and validates filename, media type, signature, and metadata;
the middleware is an early resource guard, not a replacement for application validation.

Research requests default to the current eligible archive state. Historical `PUBLIC_KNOWLEDGE` and `PERSONAL_ARCHIVE` requests require an explicit cutoff, while `CURRENT` requests must not provide one. The API boundary rejects inconsistent combinations before invoking an application service.

Before evidence validation completes, research streaming may emit progress only. Statement and citation events represent already accepted output. The authoritative HTTP surface is defined in [`docs/08-m0-0-implementation-plan.md`](../../../../docs/08-m0-0-implementation-plan.md#41-http-surface).
