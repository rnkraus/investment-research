# Investment Research Archive

Investment Research Archive is an evidence-grounded research application. It will import research documents, preserve their originals, make them searchable, and answer questions with exact, verifiable citations.

The completed M0.0 milestone is a local-only proof of the smallest end-to-end path. M0 is the active
engineering-foundation milestone and replaces the development storage, processing delivery, and
parser boundary without rebuilding the proven application contracts. Until its isolation gate
passes, use only the included synthetic fixture for accepted processing and research, as recorded in
[ADR-0004](docs/adr/0004-m0-0-parser-isolation-boundary.md). This is not yet a production deployment,
trading system, or investment-advice application.

## Prerequisites

- Node.js 24
- pnpm 11.24
- Python 3.14
- uv 0.12.7 or newer within the 0.12 release line
- Docker with Docker Compose

The pinned package-manager and runtime versions are also recorded in `package.json`, `.nvmrc`, `.python-version`, and `backend/pyproject.toml`.

## Install

Install the frontend dependencies from the repository root:

```shell
pnpm install --frozen-lockfile
```

Install the backend dependencies:

```shell
cd backend
uv sync --locked
cd ..
```

Backend dependency synchronization performs the configured fail-closed malware check against OSV and therefore requires the OSV service to be reachable.

Create the local Docker Compose configuration:

```shell
cp .env.example .env
```

Keep the host-runtime and all four PostgreSQL entries, and replace the password placeholder in
`.env` with a local-only password. The file is ignored by Git and must never be committed. Docker
Compose and local database-capable backend processes require the complete PostgreSQL configuration;
an explicitly configured process environment variable takes precedence over the file.

`INVESTMENT_RESEARCH_RUNTIME_LOCATION=host` keeps local API binds and database targets on loopback.
Compose later overrides this value separately inside each container rather than changing the host
development file.

## Run locally

Start PostgreSQL from the repository root:

```shell
docker compose up -d postgres
docker compose ps
```

Before starting the API for the first time, migrate the database and create the deterministic local
workspace:

```shell
cd backend
uv run alembic -c alembic.ini upgrade head
uv run python -m scripts.seed_m0_0
cd ..
```

Start the API in a second terminal:

```shell
cd backend
uv run uvicorn investment_research.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

The API loads `ApiSettings`, derives a validated loopback database URL from the root `.env`, creates
only its process-owned database and application resources during startup, and releases them during
shutdown. The separate processing runner loads `WorkerSettings`. An explicit
`INVESTMENT_RESEARCH_DATABASE_URL` remains available for CI and isolated tooling and overrides the
local file. Database URL query parameters are rejected so driver options cannot redirect a
validated target. Application startup does not create or migrate tables automatically.

The API writes one structured JSON completion or failure event per HTTP request at the configured
`INVESTMENT_RESEARCH_LOG_LEVEL`. It generates the correlation identifier on the server and returns
it in the `x-request-id` response header and safe JSON error envelope. Request bodies, query strings,
raw unmatched paths, headers, credentials, exception messages, and traceback text are excluded from
these application events.

Start the web application in a third terminal from the repository root:

```shell
pnpm dev
```

Open the web application at <http://127.0.0.1:3000>. The API liveness endpoint is available at
<http://127.0.0.1:8000/health>; it reports only whether the API process is running. The readiness
endpoint at <http://127.0.0.1:8000/ready> returns `200` only when PostgreSQL is reachable and its
Alembic revision exactly matches the schema required by this API version. A failed readiness check
returns only `503 {"status":"not_ready"}` and never exposes dependency or error details.

Complete the M0.0 browser journey with the synthetic fixture:

1. In the import form, choose
   `fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf`. The synthetic title,
   publication date, and English language metadata are already selected.
2. Import the document. The API preserves the verified immutable original, creates a canonical
   processing run, and returns without parsing the PDF in the request process.
3. Copy the displayed local processing command and run it in a separate repository-root terminal.
   The page polls the canonical run status and enables research after it reaches `LEXICAL_READY`.
4. Submit the prefilled revenue question. The expected validated answer reports FY2025 revenue of
   $184.2 million, up 22.0% from $151.0 million in FY2024.
5. Open the supporting citation. Its exact passage is displayed in the answer, and the protected
   original opens on page 2 in a new browser tab.
6. Select the insufficient-evidence example and submit it. The result must explicitly report missing
   evidence and contain no answer statements or citations.

The displayed runner processes exactly one captured run and then exits. It resolves the local
workspace on the server, uses its own database sessions, limits wall-clock processing time and
virtual memory, and never executes PDF parsing in FastAPI. A successful command reports
`LEXICAL_READY`; a failed command returns a non-zero exit code without printing parser or database
internals. The default bounds are 30 seconds and 512 MiB of additional virtual memory above the
loaded runner runtime. Override them only within the validated safety ceilings with
`INVESTMENT_RESEARCH_PROCESSING_TIMEOUT_SECONDS` and
`INVESTMENT_RESEARCH_PROCESSING_MEMORY_LIMIT_BYTES`.

All browser requests use the same-origin Next.js API boundary. The original and citation endpoints
recheck the server-owned workspace, active document version, validation state, byte count, and
checksum before returning source content; the browser never receives workspace credentials or
connects directly to PostgreSQL.

Stop the foreground processes with `Ctrl+C`, then stop PostgreSQL without deleting its named volume:

```shell
docker compose down
```

## Verify changes

Run the frontend checks from the repository root:

```shell
pnpm format
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

Run the backend checks from `backend/`:

```shell
uv run --locked python -m scripts.verify_backend
```

For focused feedback while editing, pass one or more backend Python files. Pytest runs only for
selected `test_*.py` files:

```shell
uv run --locked python -m scripts.verify_paths \
  src/investment_research/modules/evidence/persistence.py \
  tests/unit/test_evidence_persistence.py
```

With Docker running, execute the complete PostgreSQL verification in an automatically created and
removed disposable environment:

```shell
cd backend
uv run --locked python -m scripts.verify_postgres
```

## Run database migrations and seed data

With PostgreSQL running and the root `.env` configured, migrate the schema and create the two
deterministic synthetic M0.0 workspaces:

```shell
cd backend
uv run alembic -c alembic.ini upgrade head
uv run python -m scripts.seed_m0_0
```

Repeated seed runs are safe: identical workspaces remain unchanged, while conflicting data under a
deterministic seed identifier aborts without being overwritten. Verify that the migrated schema still
matches the SQLAlchemy metadata:

```shell
uv run alembic -c alembic.ini check
uv run alembic -c alembic.ini current
```

CI runs the deterministic frontend and backend checks, migrates an empty PostgreSQL 17 service,
applies the synthetic seed, runs all PostgreSQL integration tests, checks metadata drift, and proves
the reviewed migration can downgrade to base and upgrade back to head.

## Update API contracts

Pydantic models in `backend/src/investment_research/api/contracts.py` are the source of truth. Generated files under `frontend/src/lib/api/generated/` must not be edited manually.

After changing an API contract, regenerate both artifacts:

```shell
cd backend
uv run python scripts/export_openapi.py
cd ..
pnpm --filter @investment-research/frontend contracts:generate
```

Verify each side independently:

```shell
cd backend
uv run python scripts/export_openapi.py --check
cd ..
pnpm --filter @investment-research/frontend contracts:check
```

The following security checks depend on external registries and are currently run manually:

```shell
pnpm audit
pnpm audit signatures
cd backend
uv audit --locked
```

## Current architecture

- `frontend/` contains the Next.js, TypeScript, and Tailwind CSS web application.
- `backend/` contains the shared FastAPI and single-job processing application package and its tests.
- `compose.yaml` provides a loopback-only PostgreSQL service for local development.
- The API lifecycle owns one SQLAlchemy engine and session factory. Module repositories use the
  reviewed Alembic-managed PostgreSQL schema and caller-owned transaction boundaries.

## Project documentation

The compact project specifications are the sources of truth:

- [Product scope](docs/01-product-scope.md)
- [System design](docs/02-system-design.md)
- [Domain model](docs/03-domain-model.md)
- [Security and operations](docs/04-security-and-operations.md)
- [Delivery plan](docs/05-delivery-plan.md)
- [Future features](docs/06-future-features.md)
- [M0.0 architecture flows](docs/07-m0-0-architecture-flows.md)
- [M0.0 implementation plan](docs/08-m0-0-implementation-plan.md)
- [M0 implementation plan](docs/09-m0-implementation-plan.md)

Accepted architectural decisions are recorded in [`docs/adr/`](docs/adr/).
