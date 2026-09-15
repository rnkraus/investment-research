# Backend Scripts

This directory contains small, reviewable development utilities. Scripts support the application but are not imported by runtime request handlers.

## Contents

| File                         | Responsibility                                                                           |
| ---------------------------- | ---------------------------------------------------------------------------------------- |
| `__init__.py`                | Makes development commands importable for tests and `python -m` execution.               |
| `docker_secrets.py`          | Creates temporary mode-`0600` files for disposable Compose secrets.                      |
| `export_openapi.py`          | Exports the FastAPI OpenAPI document or verifies that the committed snapshot is current. |
| `seed_m0_0.py`               | Idempotently creates the two deterministic synthetic M0.0 workspaces.                    |
| `verify_backend.py`          | Runs all deterministic backend static checks and unit tests.                             |
| `verify_backend_images.py`   | Builds and exercises every backend image target in an isolated runtime.                  |
| `verify_object_storage.py`   | Exercises SeaweedFS S3 contracts in an isolated internal Docker network.                 |
| `verify_parser_isolation.py` | Exercises the final parser image, protocol, resources, and network isolation.            |
| `verify_paths.py`            | Runs deterministic checks for explicitly selected backend Python files.                  |
| `verify_postgres.py`         | Runs migrations, seeds, and integration tests in an isolated disposable PostgreSQL.      |
| `verify_queue_recovery.py`   | Proves live Celery redelivery and lease-based recovery after worker loss.                 |

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

The command uses the validated local PostgreSQL configuration from the root `.env`, while an
explicit process URL can override it for isolated tooling. It assumes Alembic has already migrated
the database to `head`. The verified command is documented in the repository
[`README.md`](../../README.md#run-database-migrations-and-seed-data).

## Python verification

```mermaid
flowchart LR
    ALL[verify_backend.py] --> FORMAT[Ruff format check]
    PATHS[verify_paths.py<br/>selected files] --> FORMAT
    FORMAT --> LINT[Ruff lint]
    LINT --> TYPES[Mypy]
    TYPES --> UNIT[Applicable unit tests]
```

Both commands are read-only checks: they report formatting differences but never rewrite source
files. `verify_backend.py` checks the complete backend and all unit tests. `verify_paths.py` accepts
one or more existing `.py` files inside the backend, removes duplicate paths, and passes only selected
`tests/**/test_*.py` modules to Pytest. It rejects missing, non-Python, and out-of-backend paths.

Run the complete deterministic backend check from `backend/`:

```shell
uv run --locked python -m scripts.verify_backend
```

For focused feedback while editing, provide both implementation and test files when applicable:

```shell
uv run --locked python -m scripts.verify_paths \
  src/investment_research/modules/evidence/persistence.py \
  tests/unit/test_evidence_persistence.py
```

Focused verification is not the final completion gate. Run the complete backend command after the
work block and the PostgreSQL command after persistence or migration changes.

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
password, writes it to a temporary mode-`0600` Compose secret file, and uses a unique Compose
project name. Cleanup runs after success, command failure, or test failure. It removes only
resources owned by that invocation and never targets the normal M0.0 development database or its
volume. The temporary secret directory is removed when the command exits.

## Isolated object-storage verification

```mermaid
flowchart LR
    COMMAND[verify_object_storage.py] --> IMAGE[Locked non-root verification image]
    IMAGE --> COMPOSE[Unique PostgreSQL, Redis, and SeaweedFS Compose project]
    COMPOSE --> MIGRATE[Alembic migration and workspace seed]
    COMPOSE --> BOOTSTRAP[Private bucket bootstrap]
    BOOTSTRAP --> CONTRACTS[Direct S3 and isolation contracts]
    MIGRATE --> API[API capture and authorized original-open contract]
    CONTRACTS --> API
    API --> WRITE[Write persistence probe]
    WRITE --> RESTART[Restart storage services]
    RESTART --> READ[Read persistence probe]
    READ --> CLEANUP[Remove project, volumes, and test image]
```

Run the complete SeaweedFS gate from `backend/` with Docker running:

```shell
uv run --locked python -m scripts.verify_object_storage
```

The command generates disposable PostgreSQL and Redis passwords, administrator and application
storage credentials, a private bucket, a unique Compose project, and a unique verification image.
Direct S3 tests reach only the gateway through the internal `storage-data` network. A separately
confined API contract container joins that network and the project's backend network, captures a
synthetic PDF through `POST /api/v1/documents`, publishes its processing task to Redis, and reopens
the immutable bytes through the authorized original endpoint.
SeaweedFS publishes no host port. Tests cover immutable conditional creation under concurrency,
checksum and byte integrity, open and cleanup, missing/foreign non-disclosure, workspace key scope,
least-privilege application credentials, and restart persistence. Runtime inspection also verifies
non-root users, dropped capabilities, `no-new-privileges`, read-only control-service filesystems,
unpublished storage ports, secret-file delivery, and control-plane network isolation.

The command does not load repository `.env` values. It migrates and seeds its own PostgreSQL volume;
generated secrets are mounted from temporary mode-`0600` files and their values never appear in
displayed command arguments. Cleanup always targets only that invocation's unique project,
networks, volumes, contract container, verification image, and temporary secret directory,
including after a failed build or test.

## Parser-isolation verification

```mermaid
flowchart LR
    COMMAND[verify_parser_isolation.py] --> IMAGES[Build worker and parser images]
    IMAGES --> COMPOSE[Start Redis and parser on separate internal networks]
    COMPOSE --> INSPECT[Inspect user, mounts, capabilities, and resource limits]
    INSPECT --> NEGATIVE[Probe secret, filesystem, backend, and internet denial]
    NEGATIVE --> VALID[Parse the frozen PDF through the private protocol]
    VALID --> LIMITS[Reject request and extracted-text limit violations]
    LIMITS --> CLEANUP[Remove project, volumes, images, and secret files]
```

Run the parser boundary gate from `backend/` with Docker running:

```shell
uv run --locked python -m scripts.verify_parser_isolation
```

The parser joins only the internal `parser` network; the worker is the sole bridge to the separate
backend and storage-data networks. Runtime inspection requires the declared non-root identity,
read-only root filesystem, dropped capabilities, `no-new-privileges`, exact CPU, memory, and process
limits, bounded temporary storage, no published ports, no application secrets, and no Docker socket
or repository mount. Active probes verify that `/app` is not writable, `/tmp` remains usable, and
connections to Redis and the external network fail. A worker-image probe must still parse the
repository-owned PDF through `POST /internal/v1/parse`, while request-size and extracted-text limits
fail with typed, safe outcomes. The unique Compose project and temporary secrets are removed even
when a check fails.

## Queue-recovery verification

```mermaid
flowchart LR
    COMMAND[verify_queue_recovery.py] --> STACK[Start isolated worker runtime]
    STACK --> CAPTURE[Capture fixture and publish task]
    CAPTURE --> CLAIM[Wait for PARSING claim]
    CLAIM --> KILL[Kill Celery pool worker]
    KILL --> REDELIVER[Broker redelivery]
    REDELIVER --> RECLAIM[Reclaim after lease expiry]
    RECLAIM --> READY[Require one LEXICAL_READY output]
    READY --> CLEANUP[Remove project and volumes]
```

Run the live worker-loss gate from `backend/` with Docker running:

```shell
uv run --locked python -m scripts.verify_queue_recovery
```

The command creates a unique Compose project with generated disposable credentials and does not
load repository `.env` values. It pauses the isolated parser, captures the frozen PDF through the
real application container, and waits until the Celery pool worker owns the canonical processing
lease. It then asks Celery for the exact pool-process identifier and kills only that child process,
leaving the Celery main process alive to reject and redeliver the unacknowledged message. The parser
is resumed after the deliberately shortened disposable lease expires.

Success requires the redelivered task to reach `LEXICAL_READY` on its second canonical attempt with
exactly one expected set of structure blocks, chunks, and chunk-set output. Cleanup removes the
unique project and its volumes after both success and failure.

## Backend image verification

```mermaid
flowchart LR
    COMMAND[verify_backend_images.py] --> BUILD[Build API, worker, parser]
    BUILD --> METADATA[Check target metadata and secret absence]
    METADATA --> RUNTIME[Run minimal non-root smoke tests]
    RUNTIME --> POSTGRES[Create isolated PostgreSQL]
    POSTGRES --> MIGRATE[Alembic upgrade to head]
    MIGRATE --> API[Start API image]
    API --> HEALTH[Check Docker health, /health, and /ready]
    HEALTH --> CLEANUP[Remove images, container, network, and volume]
```

Run the complete image gate from `backend/` with Docker running:

```shell
uv run --locked python -m scripts.verify_backend_images
```

The command builds the `api`, `worker`, and `parser` targets from the committed lockfile. For every
target it verifies the declared non-root user, exact entry point, expected healthcheck, minimal
runtime contents, read-only application source for the runtime user, and the absence of forbidden
credential names from image configuration and build history.

It then creates a uniquely named PostgreSQL Compose project with a random disposable password,
migrates the empty database to Alembic `head`, and supplies the API database URL only through the
container's runtime environment. PostgreSQL receives its password through a temporary mode-`0600`
secret file. The API must become healthy and return the exact public liveness and readiness
responses. Displayed commands contain environment-variable names but never their values. Cleanup
removes only the resources, temporary image tags, and secret directory owned by that invocation,
including after a failed build or runtime check.
