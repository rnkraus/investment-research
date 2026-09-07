# Backend Tests

This directory verifies deterministic application behavior and architectural boundaries without live paid providers.

## Structure

| Path           | Responsibility                                                                                    |
| -------------- | ------------------------------------------------------------------------------------------------- |
| `unit/`        | Fast tests for settings, health, identity, contracts, OpenAPI drift, and module dependency rules. |
| `integration/` | Opt-in PostgreSQL tests for migrations, seeds, persistence, constraints, and isolation.           |
| `acceptance/`  | Added for the complete M0.0 fixture journey.                                                      |

## Current coverage

```mermaid
flowchart TD
    TESTS[Backend test suite] --> RUNTIME[Settings and health]
    TESTS --> IDENTITY[Workspace isolation boundary]
    TESTS --> CONTRACTS[API and stream validation]
    TESTS --> OPENAPI[Committed contract drift]
    TESTS --> ARCH[Module import rules]
    TESTS --> POSTGRES[Opt-in PostgreSQL integration]
    POSTGRES --> SEED[Deterministic workspace seed]
```

Tests use application factories and explicit test settings. They must not read secret environment files, depend on private documents, contact paid providers, or weaken workspace and evidence rules merely to pass.

PostgreSQL integration tests are skipped unless `INVESTMENT_RESEARCH_DATABASE_URL` is explicitly
configured. They additionally require `INVESTMENT_RESEARCH_ENVIRONMENT=test`, use a schema already
migrated by Alembic, and never create tables implicitly.

Run commands are documented in the repository [`README.md`](../../README.md#verify-changes). Database
tests must prove both same-workspace access and cross-workspace non-disclosure.
