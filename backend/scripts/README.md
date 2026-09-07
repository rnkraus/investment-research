# Backend Scripts

This directory contains small, reviewable development utilities. Scripts support the application but are not imported by runtime request handlers.

## Contents

| File                | Responsibility                                                                           |
| ------------------- | ---------------------------------------------------------------------------------------- |
| `__init__.py`       | Makes development commands importable for tests and `python -m` execution.                |
| `export_openapi.py` | Exports the FastAPI OpenAPI document or verifies that the committed snapshot is current. |
| `seed_m0_0.py`      | Idempotently creates the two deterministic synthetic M0.0 workspaces.                    |
| `verify_postgres.py` | Runs migrations, seeds, and integration tests in an isolated disposable PostgreSQL.     |

## Contract flow

```mermaid
flowchart LR
    PYDANTIC[Pydantic contracts] --> FASTAPI[FastAPI OpenAPI]
    FASTAPI --> EXPORT[export_openapi.py]
    EXPORT --> JSON[generated/openapi.json]
    JSON --> TYPES[generated/schema.d.ts]
```

The script uses test settings, produces stable sorted JSON, and writes to `frontend/src/lib/api/generated/openapi.json` by default. Its `--check` mode does not rewrite the snapshot; it fails when the committed file is missing or stale.

Generated artifacts must not be edited manually. The complete command sequence is documented in the repository [`README.md`](../../README.md#update-api-contracts).

## Seed flow

```mermaid
flowchart LR
    SETTINGS[Validated local settings] --> ENGINE[PostgreSQL engine]
    ENGINE --> TX[One transaction]
    FIXTURES[Deterministic workspace definitions] --> REPOSITORY[Workspace repository]
    TX --> REPOSITORY
    REPOSITORY --> WORKSPACES[(workspaces)]
```

The seed command uses the same local workspace identifier as the fixed M0.0 identity provider and
adds a second sanitized workspace for negative isolation tests. Repeated runs leave identical rows
unchanged. An existing deterministic identifier with different data aborts and rolls back the whole
seed transaction instead of overwriting data.

The command requires an explicit validated local PostgreSQL URL and assumes Alembic has already
migrated the database to `head`. The verified command is documented in the repository
[`README.md`](../../README.md#run-database-migrations-and-seed-data).

## Isolated PostgreSQL verification

```mermaid
flowchart LR
    COMMAND[verify_postgres.py] --> DOCKER[Unique Docker Compose project]
    DOCKER --> MIGRATE[Alembic upgrade]
    MIGRATE --> SEED[M0.0 seed]
    SEED --> TESTS[PostgreSQL integration tests]
    TESTS --> DRIFT[Alembic check]
    DRIFT --> CYCLE[Downgrade and upgrade]
    CYCLE --> CLEANUP[Remove container and volume]
```

The verification command selects an available loopback port, generates a random disposable
password, and uses a unique Compose project name. Cleanup runs after success, command failure, or
test failure. It removes only resources owned by that invocation and never targets the normal M0.0
development database or its volume.
