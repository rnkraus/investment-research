# Investment Research Archive

Investment Research Archive is an evidence-grounded research application. It imports research documents, preserves their originals, makes them searchable, and answers questions with exact, verifiable citations.

The completed M0.0 milestone proves the smallest end-to-end path. The completed M0 engineering
foundation replaces its development storage, processing delivery, and parser shortcuts with a
production-shaped local runtime while preserving the proven application contracts. The accepted M1
thin slice adds PDF and pasted-text input, versioned document embedding and dense indexing, lexical
and dense retrieval, typed answer generation, server-side evidence validation, and exact citation
opening. Live progress and independent answer/support model choices are available in the local
provider-backed runtime. The frozen M0.0 fixture remains a regression case under
[ADR-0004](docs/adr/0004-m0-0-parser-isolation-boundary.md) and
[ADR-0006](docs/adr/0006-credential-free-parser-runtime.md). M2 is the next planning milestone.
This is still a local development system, not a production deployment, trading system, or
investment-advice application. The narrow M1 acceptance evidence and limits are recorded in the
[M1 implementation plan](docs/10-m1-implementation-plan.md).

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

Install the repository-managed Git pre-commit hook from the repository root:

```shell
uv run --project backend --locked pre-commit install
```

The hook invokes only local commands backed by `uv.lock` and `pnpm-lock.yaml`. It checks affected
backend, API-contract, and frontend files without rewriting them. CI executes the same hooks on all
tracked files, so skipping the local hook never skips the authoritative checks.

Create the local Docker Compose configuration:

```shell
cp .env.example .env
install -d -m 700 .secrets
install -m 600 /dev/null .secrets/postgres_password
install -m 600 /dev/null .secrets/redis_password
install -m 600 /dev/null .secrets/seaweedfs_admin_access_key
install -m 600 /dev/null .secrets/seaweedfs_admin_secret_access_key
install -m 600 /dev/null .secrets/seaweedfs_application_access_key
install -m 600 /dev/null .secrets/seaweedfs_application_secret_access_key
```

Keep the host-runtime and all four PostgreSQL entries, and replace the password placeholder in
`.env` with a local-only password. The file is ignored by Git and must never be committed. Docker
Compose and local database-capable backend processes require the complete PostgreSQL configuration;
an explicitly configured process environment variable takes precedence over the file.

Use a local editor or password manager to put exactly one non-empty value in each ignored secret
file. Do not include a variable name or surrounding quotes. Use distinct, random values with at
least 16 characters; never commit or reuse them outside local development. Normal Compose services
read PostgreSQL, Redis, and object-storage credentials only from their mounted secret files. Direct
values in the ignored `.env` are retained only for explicit host-side development or verification
tools and must match the corresponding secret file whenever they target the same disposable service.

The non-secret `*_SECRET_FILE` entries in `.env` select these source paths for Docker Compose.
Inside a container, Compose mounts the selected file under `/run/secrets`; the settings layer reads
the value there without placing it in the container environment or process arguments.

For the explicitly credentialed local M1 stack, create five more ignored files and put one
role-specific provider key in each using a local editor or password manager:

```shell
umask 077
touch .secrets/openai_query_api_key .secrets/pinecone_query_api_key
touch .secrets/openai_generation_api_key .secrets/openai_document_api_key
touch .secrets/pinecone_index_api_key
chmod 600 .secrets/openai_query_api_key .secrets/pinecone_query_api_key
chmod 600 .secrets/openai_generation_api_key .secrets/openai_document_api_key
chmod 600 .secrets/pinecone_index_api_key
```

`touch` and `chmod` leave any existing file contents untouched. Do not paste keys into shell
commands or terminal output.
Set `INVESTMENT_RESEARCH_PROVIDER_MONTHLY_BUDGET_MICRO_USD=5000000` and the hostname-only
`INVESTMENT_RESEARCH_PINECONE_INDEX_HOST` in the ignored `.env`. Do not put the five provider keys
there for Compose: `compose.m1-live.yaml` mounts their files only into the process that needs each
key. The optional `*_SECRET_FILE` entries in `.env.example` select alternative source paths; the
default names above need no extra path configuration. Existing direct provider-key entries in a
host `.env` are not passed into containers by `--env-file`; move their values to the matching
ignored files if switching from host development to live Compose. Keep live-provider use limited to
approved public or synthetic material; this local single-user stack is not a public deployment.

`INVESTMENT_RESEARCH_RUNTIME_LOCATION=host` remains available for focused host-side tools and tests.
The web application, API, Celery worker, and parser run as Compose services. Compose supplies their
container-specific database, broker, S3, parser, and role-specific provider settings without
placing credential values in container environment variables.

## Run locally

Start the complete provider-backed local M1 runtime from the repository root:

```shell
docker compose --env-file .env -f compose.yaml -f compose.m1-live.yaml up --build --detach --wait
docker compose --env-file .env -f compose.yaml -f compose.m1-live.yaml ps
```

Compose waits for PostgreSQL, runs the reviewed one-shot migration to Alembic `head`, initializes the
SeaweedFS bucket, and starts the API, worker, and web application only after their required
dependencies are ready. It does not seed or delete business data during restart. Use the explicit
seed or reset commands below when deterministic synthetic workspaces are required. The base
`compose.yaml` remains the credential-free foundation for isolated tests; by itself it does not
configure a working M1 indexing worker.

### Opt-in M1.5 source-ID research

M1 remains the default research policy. With the provider-backed local stack above already
running, rebuild and restart only the API to test the opt-in RRF/source-unit/document-attribution
path without editing `.env`:

```shell
INVESTMENT_RESEARCH_RESEARCH_PROFILE=m1-5-citable-units \
  docker compose --env-file .env -f compose.yaml -f compose.m1-live.yaml up --build --detach --wait api
```

The live overlay forwards this non-secret selection only to the API. The model selects opaque
source-unit IDs instead of copying quotes; the server resolves their exact original ranges and
still validates every claim. All four answer/support model pairs and the existing monthly budget
remain available. Existing `SEARCHABLE` sources need no upload, migration, or reindexing merely
because the research profile changes. Worker and parser behavior is unchanged.

Use the existing website to ask one answerable question about an approved public/synthetic PDF and
one about pasted text. Open each citation: the PDF must open at the correct physical page and the
text viewer must highlight the exact original range, including repeated text. Normal provider
charges apply to these owner-run checks; selecting the profile does not start a paid model request.
Raw diagnostics remain off unless the separate local-only override below is explicitly enabled.
The source-opening check and a later default-policy decision remain pending in the
[M1.5 plan](docs/13-m1-5-rag-quality-plan.md#live-compose-selection-2026-10-08).

If source-unit preparation exceeds a bound, the website reports `RESEARCH_CONTEXT_LIMIT` with
a fixed suggestion to narrow the question or select a single document. Inspect the content-free
diagnostic without enabling raw model output:

```shell
docker compose --env-file .env -f compose.yaml -f compose.m1-live.yaml logs --since 15m --no-color api \
  | grep '"event":"research.context_limit.exceeded"'
```

The event includes the HTTP request ID, `limit_kind`, `observed_value`, and `limit_value`, never
source text or credentials. Kinds are `source_count`, `context_characters`, `unit_count`, and
`input_metadata`. The unit counter records the first rejected unit, not the total possible units;
metadata counters are conservative input-accounting units, not measured provider tokens. This
diagnostic does not raise limits or silently discard evidence. Rebuild both API and web when
updating to this error-contract version so the web client recognizes the new code.

To return to the retained M1 policy without changing or deleting archive data:

```shell
INVESTMENT_RESEARCH_RESEARCH_PROFILE=m1 \
  docker compose --env-file .env -f compose.yaml -f compose.m1-live.yaml up --detach --wait api
```

The API writes one structured JSON completion or failure event per HTTP request at the configured
`INVESTMENT_RESEARCH_LOG_LEVEL`. It generates the correlation identifier on the server and returns
it in the `x-request-id` response header and safe JSON error envelope. Request bodies, query strings,
raw unmatched paths, headers, credentials, exception messages, and traceback text are excluded from
these application events.

For an explicitly approved public or synthetic document, an opt-in local diagnostic can show raw
provider output when answer generation or statement-support response parsing fails. It also shows
the unvalidated answer proposal and failed rules when server-side evidence validation rejects an
answer. Start the API with the additional override, then watch its terminal logs:

```shell
docker compose --env-file .env -f compose.yaml -f compose.m1-live.yaml -f compose.m1-local-diagnostics.yaml up --build --detach --wait api
docker compose --env-file .env -f compose.yaml -f compose.m1-live.yaml -f compose.m1-local-diagnostics.yaml logs --follow api
```

Look for `local.model_failure_output` with `role` (`answer`, `statement_support`, or
`evidence_validation`), `model`, and `output_text`. For `answer` with `quote_not_found`, the nested
JSON identifies the first missing model-proposed `exact_text`, its `retrieval_candidate_id`, and
zero-based `statement_ordinal` and `evidence_ordinal`. It does not include the candidate's source
text; the quote remains unverified until it matches that source exactly. For `evidence_validation`,
the nested JSON has the proposed rendering, rejected statements and proposed citation text, and
failed validation findings. A `STATEMENT_SUPPORT` failure means the support decision did not accept
the statement as fully supported; it does not expose a model explanation. The proposal and citation
text remain untrusted and are not sent to the browser or stored as accepted answer content. Output
is capped at 100,000 characters and may contain source text. Do not use this override with private documents
or in CI, staging, or public deployments; do not paste raw output into tickets or chats. Connection
failures without a model response have no output text to show. Docker may retain these logs after
the request. After debugging, restart without the override to disable the output:

```shell
docker compose --env-file .env -f compose.yaml -f compose.m1-live.yaml up --build --detach --wait api
```

Research may make several bounded provider calls before its first response event. It streams safe
progress events while work continues; statements and citations follow only after validation. The
local Next.js rewrite allows up to 120 seconds for the complete request instead of its previous
30-second default. If the web
container reports a proxy disconnect, the API may still finish after the browser has received an
error; do not immediately repeat a paid request without checking its outcome. The research UI shows
a safe HTTP or connection explanation and any available request ID. See the
[frontend API boundary](frontend/src/lib/api/README.md) for the error-handling contract.

Open the web application at <http://127.0.0.1:3000>. The API liveness endpoint is available at
<http://127.0.0.1:8000/health>; it reports only whether the API process is running. The readiness
endpoint at <http://127.0.0.1:8000/ready> returns `200` only when PostgreSQL is reachable and its
Alembic revision exactly matches the schema required by this API version, Redis accepts the Celery
broker connection, and the configured S3 bucket is accessible. A failed readiness check returns only
`503 {"status":"not_ready"}` and never exposes dependency or error details.

The web application presents the automatic processing states and no longer exposes the temporary
M0.0 runner command. Its non-root, read-only Distroless container reaches only the API through the
frontend network. Browser API requests remain same-origin; PostgreSQL, Redis, object storage, and
the parser are not reachable from the web container.

In the provider-backed M1 runtime, the research form offers independent answer-generation and
statement-support model selections: `gpt-5.6-terra` or `gpt-5.6-luna`, with Terra as the default for
both. The browser obtains the available choices from FastAPI and sends only model identifiers; API
credentials stay server-side. If the choices cannot be loaded, research submission is disabled
rather than silently using another model. The deterministic runtime has no paid-model controls.
The research form is available without a new upload: its default searches eligible saved documents
in the local workspace. After an import reaches `SEARCHABLE`, the user can optionally restrict the
question to that document. This is not yet an archive browser or document-management workflow.
After a frontend code change, rebuild the Compose web image with the `up --build` command above
before checking the served website.

Use the documented complete automatic M0 fixture verification below to exercise the containerized
web application, its same-origin API proxy, API capture, Redis/Celery delivery, SeaweedFS, isolated
parsing, research, citation resolution, original access, and the insufficient-evidence case
together. The final local verification and hosted `Complete M0 runtime` CI gate passed; M0 is
complete. The complete M1 runtime verification instructions appear below.

All browser requests use the same-origin Next.js API boundary. The original and citation endpoints
recheck the server-owned workspace, active document version, validation state, byte count, and
checksum before returning source content; the browser never receives workspace credentials or
connects directly to PostgreSQL.

For focused frontend development, stop only the Compose web service, keep the backend services
running, and start the local development server:

```shell
docker compose --env-file .env -f compose.yaml -f compose.m1-live.yaml stop web
pnpm dev
```

Stop the Compose stack without deleting its named volumes:

```shell
docker compose --env-file .env -f compose.yaml -f compose.m1-live.yaml down
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

Run every configured pre-commit hook manually from the repository root:

```shell
uv run --project backend --locked pre-commit run --all-files --show-diff-on-failure
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

Verify the SeaweedFS S3 contracts in a separate internal Docker environment. The command creates a
migrated disposable PostgreSQL database, generates disposable credentials, captures and reopens an
original through the real API contract, checks concurrency, isolation, permissions, and restart
persistence, and removes its unique containers, networks, volumes, and verification image:

```shell
uv run --locked python -m scripts.verify_object_storage
```

Verify the pinned non-root backend image targets from `backend/`. This command builds the API,
migration, seed, worker, and parser targets, checks their runtime contents and image metadata,
starts the API against a freshly migrated disposable PostgreSQL database, verifies `/health` and
`/ready`, and removes all resources it created:

```shell
uv run --locked python -m scripts.verify_backend_images
```

Verify the parser's final runtime boundary from `backend/`. This command starts an isolated parser
and Redis, inspects container restrictions, actively checks denied secret, filesystem, backend, and
external-network access, parses the frozen PDF through the worker-to-parser protocol, verifies safe
limit failures, and removes its disposable resources:

```shell
uv run --locked python -m scripts.verify_parser_isolation
```

Verify the complete automatic M0 fixture journey from `backend/`. The command creates a fresh
disposable runtime with random loopback ports and credentials, starts the containerized web, API,
worker, parser, PostgreSQL, Redis, and SeaweedFS services, and runs a credential-free acceptance
client attached only to the frontend network. That client loads the web page and sends every upload,
status, research, evidence, and original request through the same-origin web proxy. It waits for
Celery and the isolated parser to reach `LEXICAL_READY`, verifies the validated answer, exact
citation, original bytes, and insufficient-evidence response, and removes every resource it created:

```shell
uv run --locked python -m scripts.verify_m0_runtime
```

Verify the complete credential-free M1 PDF and pasted-text journeys from `backend/`. Before the
first local run, install the Chromium build pinned to the repository's Playwright version from the
repository root:

```shell
pnpm --filter @investment-research/frontend exec playwright install chromium
```

Then run the complete gate with Docker available:

```shell
uv run --locked python -m scripts.verify_m1_runtime
```

The M1 verifier creates a fresh isolated Compose project with random loopback ports and disposable
credentials. It first exercises PDF and pasted-text capture, processing, lexical and deterministic
dense retrieval, answer validation, citation resolution, byte-identical originals, repeatability,
safe insufficiency, and prompt-injection refusal through the same-origin HTTP boundary. Playwright
then drives the visible Chromium interface through both media journeys, opens the validated PDF at
its stored physical page, and highlights the exact verified pasted-text range. The required run uses
credential-free deterministic provider implementations and incurs no OpenAI or Pinecone spend. It
scans application logs for protected values and removes its containers, networks, volumes, and
uniquely tagged acceptance image after success or failure.

## Seed or reset local data

The normal Compose startup migrates the database automatically but intentionally does not seed
business data. With the stack running, create the two deterministic synthetic M0.0 workspaces from
the repository root through the short-lived internal tool container:

```shell
docker compose --env-file .env --profile tools run --build --rm --no-deps seed
```

Repeated seed runs are safe: identical workspaces remain unchanged, while conflicting data under a
deterministic seed identifier aborts without being overwritten.

To remove only the fixed local Compose project's PostgreSQL and SeaweedFS volumes, rebuild the
complete runtime, and restore the synthetic seed, run the guarded reset from `backend/`:

```shell
cd backend
uv run --locked python -m scripts.reset_local
```

The command validates the repository and working directory and proceeds only after the exact
displayed confirmation phrase is entered. It cannot accept an arbitrary project name or deletion
path. The reset permanently deletes data in the named local project's volumes; it does not target
other Compose projects. Do not use it for shared or production data.

For a non-destructive schema, seed, drift, and migration-cycle proof against a disposable database,
run `uv run --locked python -m scripts.verify_postgres` from `backend/`.

CI runs the deterministic frontend and backend checks, migrates an empty PostgreSQL 17 service,
applies the synthetic seed, runs all PostgreSQL integration tests, checks metadata drift, and proves
the reviewed migration can downgrade to base and upgrade back to head. A separate image job validates
the Dockerfile and Compose configuration, then builds and exercises every backend target through the
same disposable API-readiness gate used locally. The processing-runtime job exercises the
credential-free parser boundary and real Celery/Redis recovery after forced pool-worker loss in
separate disposable Compose projects with generated credentials and automatic cleanup. It then
runs the complete automatic fixture journey through capture, processing, research, exact evidence,
repeated-answer evidence reuse, original access, and the insufficient-evidence control case. The
journey scans captured application logs for injected credentials and fixture evidence text. An
`always()` final step fails the job if any scoped verifier container, network, volume, or temporary
acceptance image remains. Configure the stable `Complete M0 runtime` check as required in branch
protection; workflow YAML cannot enforce that repository setting.

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
- `backend/` contains the shared FastAPI, Celery worker, parser, and application package and tests.
- `backend/Dockerfile` provides reproducible non-root API, Celery worker, and private parser targets.
- `compose.yaml` provides PostgreSQL, authenticated Redis, the Celery worker, the credential-free
  parser, the API, and internally isolated SeaweedFS master, volume, filer, S3 gateway, and one-shot
  bucket bootstrap services. PostgreSQL has no normal host port and the backend network is internal.
  The API alone bridges the loopback-facing frontend network to backend and storage-data; the worker
  alone bridges backend, storage-data, and parser. `compose.test.yaml` adds a separate test-only
  bridge and loopback database port for isolated host-driven verification. `compose.m1-live.yaml`
  adds the approved live M1 provider configuration, role-scoped secret mounts, and a dedicated
  outbound network for the worker's OpenAI and Pinecone calls. The parser stays on its internal
  network; this outbound network is not a domain allowlist.
- `.secrets/` is an ignored local source directory for Compose secret mounts; it is not application
  configuration, a production secret manager, or a place for shared credentials.
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
- [M1 implementation plan](docs/10-m1-implementation-plan.md)
- [M1 contract freeze](docs/11-m1-contracts.md)
- [M1.5 RAG quality proof plan (draft)](docs/13-m1-5-rag-quality-plan.md)
- [M2 implementation plan (draft)](docs/12-m2-implementation-plan.md)

Accepted architectural decisions are recorded in [`docs/adr/`](docs/adr/).
