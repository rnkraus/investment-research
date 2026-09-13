# Frontend API Boundary

This directory owns the frontend side of the FastAPI contract. It combines generated declarations
with a small application-owned HTTP client and explicit runtime checks at the network boundary.

## Contents

| Path                     | Responsibility                                                                   |
| ------------------------ | -------------------------------------------------------------------------------- |
| `generated/openapi.json` | Deterministic snapshot exported from the FastAPI application.                    |
| `generated/schema.d.ts`  | Readonly TypeScript declarations generated from that snapshot.                   |
| `client.ts`              | Sends typed requests, validates responses, decodes SSE, and rejects unsafe URLs. |

## Contract lifecycle

```mermaid
flowchart LR
    MODELS[Backend Pydantic models] --> OPENAPI[FastAPI OpenAPI]
    OPENAPI --> SNAPSHOT[openapi.json]
    SNAPSHOT --> TYPES[schema.d.ts]
    TYPES --> CLIENT[Typed frontend client]
    CLIENT --> RUNTIME[Runtime response checks]
    RUNTIME --> UI[Feature UI]
```

Files under `generated/` must never be edited manually. Backend tests verify that `openapi.json` matches the application; the frontend contract check verifies that `schema.d.ts` matches `openapi.json`. Together they make contract drift fail automated checks.

Generation and verification commands are documented in the repository [`README.md`](../../../../README.md#update-api-contracts). API payload types must be imported from the generated schema rather than redefined by hand.

## Request path

```mermaid
flowchart LR
    UI[Browser feature] --> CLIENT[client.ts]
    CLIENT --> SAME[/api/v1 path]
    SAME --> PROXY[Next.js same-origin rewrite]
    PROXY --> API[Loopback FastAPI]
```

The browser receives only relative `/api/v1/` paths. `next.config.ts` reads the server-only
`INVESTMENT_RESEARCH_API_ORIGIN` setting and accepts only a credential-free loopback HTTP origin in
M0.0. This avoids exposing an internal target in a `NEXT_PUBLIC_` variable and avoids a permissive
CORS policy.

Successful JSON responses are checked for the fields used by the application instead of being
trusted solely because TypeScript compiled. Server-sent events are buffered across arbitrary network
chunks, decoded by their `event` discriminator, and rejected if their runtime shape does not match the
generated union. Backend-provided status and viewer URLs must remain relative application API paths.
