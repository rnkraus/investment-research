# Investment Research Archive

Investment Research Archive is an evidence-grounded research application. It will import research documents, preserve their originals, make them searchable, and answer questions with exact, verifiable citations.

The current M0.0 milestone is a local-only proof of the smallest end-to-end path. Use only public or synthetic fixtures. It is not a production deployment, trading system, or investment-advice application.

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

Keep all four PostgreSQL entries and replace the password placeholder in `.env` with a local-only
password. The file is ignored by Git and must never be committed. Docker Compose and the local
backend require the complete PostgreSQL configuration and read the same values; an explicitly
configured process environment variable takes precedence over the file.

## Run locally

Start PostgreSQL from the repository root:

```shell
docker compose up -d postgres
docker compose ps
```

Start the API in a second terminal:

```shell
cd backend
uv run uvicorn investment_research.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

The API derives a validated loopback database URL from the root `.env`, creates the process-wide
database resources during startup, and releases them during shutdown. An explicit
`INVESTMENT_RESEARCH_DATABASE_URL` remains available for CI and isolated tooling and overrides the
local file. Application startup does not create or migrate tables automatically.

Start the web application in a third terminal from the repository root:

```shell
pnpm dev
```

Open the web application at <http://127.0.0.1:3000>. The API health endpoint is available at <http://127.0.0.1:8000/health>.

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
- `backend/` contains the FastAPI application and its tests.
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

Accepted architectural decisions are recorded in [`docs/adr/`](docs/adr/).
