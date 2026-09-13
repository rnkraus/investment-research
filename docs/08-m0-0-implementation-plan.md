# Investment Research Archive — M0.0 Implementation Plan

**Status:** M0.0 implemented and locally accepted on 2026-09-13
**Scope:** Exact repository shape, contracts, build sequence, and verification for M0.0
**Related:** [02-system-design.md](./02-system-design.md), [03-domain-model.md](./03-domain-model.md), [04-security-and-operations.md](./04-security-and-operations.md), [05-delivery-plan.md](./05-delivery-plan.md), [07-m0-0-architecture-flows.md](./07-m0-0-architecture-flows.md)

## 1. Outcome and boundary

M0.0 proves one complete English-only archive journey:

```text
Upload fixture PDF → preserve original → process → lexical search → generate typed answer → validate evidence → open exact source
```

The same path must return explicit insufficient coverage for a known unanswerable question. M0.0 is local-only, uses public or synthetic fixtures, and does not claim production reliability.

M0.0 establishes reusable application contracts and the evidence path. It does not implement OIDC, Redis, Celery, MinIO, Pinecone, dense retrieval, retries, deletion, conversations, full observability, or public deployment.

## 2. Fixed implementation decisions

1. **One modular monolith:** the FastAPI API and development processing runner import the same Python application package.
2. **Acyclic modules:** API routes call application services. `Documents` requests `Processing`; `Research` depends on `Search`, `AnswerGenerator`, and `Evidence`. Return values do not create reverse imports.
3. **Canonical processing intent:** the upload transaction creates a `ProcessingRun`. The API returns accepted metadata and does no PDF parsing.
4. **Separate development runner:** a bounded, single-job runner claims a pending `ProcessingRun` and invokes the processing application service outside the API process. It has no retry, durability, or availability guarantee.
5. **One unit of work per execution:** each API request and runner execution creates and closes its own SQLAlchemy session. Sessions are never shared across requests, threads, or processes.
6. **Migrations from the first table:** Alembic owns schema changes. Application startup never calls metadata `create_all` or performs implicit schema mutation.
7. **PostgreSQL lexical baseline:** chunks have a versioned English `tsvector`, a GIN index, deterministic tie-breaking, and canonical workspace and active-version filters.
8. **API-owned contract:** Pydantic models and generated OpenAPI are the HTTP source of truth. Frontend TypeScript types are generated from or mechanically checked against that contract; payloads are not independently redefined.
9. **Replaceable generation:** `AnswerGenerator` is an application-owned port. The deterministic fixture adapter is the default. A real provider adapter is optional, separately approved, and restricted to the public fixture.
10. **Validation before answer output:** generation completes into a typed result, then server-side validation runs. Only accepted statements and citations are emitted; pre-validation streaming is limited to progress events.
11. **Server-owned workspace context:** the server resolves a fixed local development identity and workspace. The client cannot select or prove authorization with `workspace_id`.
12. **Immutable local originals:** the M0.0 storage adapter writes under a configured, Git-ignored storage root outside the web root, uses server-generated keys and atomic writes, verifies checksums, never overwrites an existing object, and refuses symbolic links at every object-key path component.

These decisions are implementation rules for M0.0. Only decisions that remain cross-cutting and expensive to reverse are promoted to ADRs after the proof provides evidence.

## 3. Repository structure

Create files only when their implementation step begins; do not scaffold empty placeholder modules.

```text
.
├── AGENTS.md
├── README.md
├── .github/workflows/ci.yml             # deterministic and PostgreSQL verification
├── compose.yaml                         # M0.0: PostgreSQL only; expanded in M0
├── package.json                         # root pnpm scripts after they exist
├── pnpm-workspace.yaml
├── pnpm-lock.yaml
├── frontend/
│   ├── package.json
│   ├── next.config.ts
│   ├── tsconfig.json
│   └── src/
│       ├── app/
│       │   ├── layout.tsx
│       │   ├── page.tsx
│       │   ├── documents/[documentId]/page.tsx
│       │   └── research/page.tsx
│       ├── features/
│       │   ├── documents/
│       │   └── research/
│       ├── components/ui/               # selected shadcn/ui source components
│       └── lib/api/
│           ├── client.ts
│           └── generated/                # generated OpenAPI types; no hand edits
├── backend/
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── scripts/
│   │   ├── export_openapi.py            # generate or verify the committed API contract
│   │   ├── seed_m0_0.py                 # idempotent synthetic workspace seed
│   │   ├── verify_backend.py            # deterministic backend gate
│   │   ├── verify_paths.py              # focused Python-file verification
│   │   └── verify_postgres.py           # disposable PostgreSQL gate
│   ├── alembic.ini
│   ├── migrations/
│   │   ├── env.py
│   │   ├── script.py.mako
│   │   └── versions/
│   │       └── 0001_m0_0_schema.py     # reviewed minimum physical schema
│   ├── src/investment_research/
│   │   ├── main.py                       # FastAPI composition root entry point
│   │   ├── runner.py                     # bounded single-job processing entry point
│   │   ├── bootstrap.py                  # dependency wiring only
│   │   ├── settings.py                   # validated, secret-safe local settings
│   │   ├── api/
│   │   │   ├── dependencies.py
│   │   │   ├── errors.py
│   │   │   └── routes/
│   │   │       ├── health.py
│   │   │       ├── documents.py
│   │   │       ├── processing.py
│   │   │       └── research.py
│   │   ├── modules/
│   │   │   ├── identity/
│   │   │   ├── documents/
│   │   │   ├── processing/
│   │   │   ├── search/
│   │   │   ├── research/
│   │   │   └── evidence/
│   │   └── infrastructure/
│   │       ├── database/                # metadata, engine, sessions, transactions
│   │       ├── object_storage/
│   │       ├── parsing/
│   │       ├── search/                  # PostgreSQL lexical-search adapter
│   │       └── generation/
│   └── tests/
│       ├── unit/
│       ├── integration/
│       └── acceptance/
├── fixtures/
│   └── m0_0/
│       ├── manifest.yaml                 # license, checksum, questions, expected spans
│       └── documents/                    # public or synthetic fixture only
└── docs/
```

Each backend module starts with only the files it needs from this set:

- `domain.py` for module-owned entities, values, and invariants;
- `contracts.py` for typed commands, queries, and results exposed to other modules;
- `application.py` for use cases and orchestration;
- `ports.py` for application-owned external interfaces;
- `persistence.py` for that module's SQLAlchemy mappings and repository implementations.

`backend/` contains the shared Python application package used by the FastAPI process, the M0.0 development runner, and the later Celery worker; it is not limited to HTTP API code. The composition root may import infrastructure implementations. Domain and application code must not import FastAPI routes or concrete provider/storage adapters. One module must not import another module's `persistence.py`.

## 4. M0.0 contracts

### 4.1 HTTP surface

| Endpoint | Purpose | Required behavior |
|---|---|---|
| `GET /health` | API process health | No private details or dependency credentials |
| `POST /api/v1/documents` | Upload the fixture PDF and metadata as multipart form data | Verify the file before capture; return document, version, and processing-run identifiers with accepted state |
| `GET /api/v1/processing-runs/{processing_run_id}` | Read processing state | Enforce server-resolved workspace and return only safe failure details |
| `POST /api/v1/research-requests` | Execute the bounded research path | Accept JSON question/context; return typed server-sent events when requested |
| `GET /api/v1/evidence-spans/{evidence_span_id}` | Resolve a validated citation | Recheck workspace and active document version; return source location and viewer target |
| `GET /api/v1/documents/{document_id}/versions/{version_id}/original` | Open the immutable original | Recheck workspace; serve only the recorded artifact with a safe media type |

The upload endpoint uses multipart fields for metadata; it does not attempt to combine a JSON request body with the file. `workspace_id` is absent from public request payloads in M0.0.

### 4.2 Validated answer stream

The research endpoint may emit:

- `research.progress` while retrieval, generation, or validation is running;
- `answer.statement` only for an accepted statement;
- `answer.citation` only for an accepted statement-to-evidence relationship;
- `answer.completed` with the final coverage state;
- `answer.insufficient` when the archive lacks sufficient evidence;
- `research.failed` with a safe typed error.

Every event includes a schema version and `research_request_id`. No raw model token or unvalidated statement is part of the stream.

The implemented synchronous M0.0 endpoint completes retrieval, generation, validation, and
persistence before constructing its SSE response. It therefore emits only validated statement and
citation events followed by one terminal event; it does not fabricate progress events after the work
has completed. `research.progress` remains in the versioned contract for a later observable
execution boundary that can report genuine stage changes.

### 4.3 Application ports retained after M0.0

| Port | M0.0 adapter | Later adapter or extension |
|---|---|---|
| `ObjectStorage` | Immutable local filesystem | MinIO in M0; managed S3-compatible storage after deployment |
| `DocumentParser` | Bounded text-PDF parser | Same port with hardened or additional parsers |
| `AnswerGenerator` | Deterministic fixture generator | Approved AI provider adapter while deterministic tests remain |
| `ProcessingExecutor` | Separate single-job development runner | Celery task invokes the same processing service in M0 |
| `LexicalSearch` | PostgreSQL full-text search | Retained and combined with dense retrieval from M1 onward |

`ProcessingExecutor` accepts a `processing_run_id`, resolves canonical state itself, and owns its unit of work. It does not accept ORM objects or an API session.

## 5. Minimum physical schema

The first Alembic history contains only tables used by the M0.0 flow:

- `workspaces`;
- `documents`, `document_versions`, and `original_artifacts`;
- `processing_runs`, `structure_blocks`, `chunk_sets`, and `chunks`;
- `research_requests`, `retrieval_runs`, and `retrieval_candidates`;
- `answers`, `answer_statements`, `evidence_spans`, `statement_evidence`, and `answer_validations`.

Conceptual entities do not automatically require one table. M0.0 omits users and memberships, companies, conversations, processing stages, jobs, outbox events, index receipts, audit, usage, and future structured-knowledge tables.

Schema rules:

- every listed product table carries `workspace_id`, even when it could be reached through another relation;
- UUID identifiers are server-generated and are not authorization proof;
- document versions and original-artifact identity fields are immutable;
- PostgreSQL triggers reject updates and deletes of `document_versions` and `original_artifacts`;
- evidence spans record document version, structure block, page, offsets, exact text, and text hash;
- chunks reference their source structure blocks and a versioned chunk set;
- `chunks.search_vector` is a `tsvector` with a GIN index;
- lexical retrieval filters by workspace and eligible active version before ranking, then uses a stable identifier as the final tie-breaker;
- configuration versions are stored for parser, chunking, lexical retrieval, answer generator, and validation behavior;
- foreign keys, uniqueness rules, and check constraints enforce relationships that the database can verify;
- repositories still enforce workspace and lifecycle eligibility because identifiers and database relations are not authorization.

Answer generation completes before the answer-persistence transaction opens. Canonical evidence
reload, validation, lifecycle transitions, statements, exact spans, relationships, and validation
records then share one answer-validation unit of work. The service explicitly flushes staged parent
records before adding their identifier-based foreign-key dependents; this orders SQL inside the same
transaction and is not an early commit. Any later failure rolls back the complete answer aggregate.

Alembic autogeneration may propose a migration, but the migration is reviewed before use. Verification upgrades an empty database to head and checks that model metadata and migration state do not drift.

## 6. Processing flow

1. The API resolves the fixed local identity and workspace.
2. It validates the upload's extension, media type, content signature, byte limit, and safe metadata.
3. The storage adapter writes to a temporary file, computes the checksum, atomically places the immutable object under a server-generated key, and refuses overwrite.
4. One database transaction creates the document, document version, original artifact, and pending `ProcessingRun`.
5. The API returns accepted identifiers and a processing-status URL.
6. The separate runner claims one pending run, opens a new database session, and applies page, memory, and execution-time limits.
7. The processing service loads the original through `ObjectStorage`, parses page-aware structure blocks, creates the versioned chunk set and chunks, writes lexical vectors, and marks the run `LEXICAL_READY`.
8. On failure it marks the run `FAILED` with a safe summary. M0.0 does not automatically retry.
9. The runner closes its session and exits. Re-running a completed run must not duplicate active output, but full at-least-once repair guarantees remain M2 scope.

## 7. Implementation sequence and gates

Follow the sequence strictly enough that each step leaves a runnable, reviewable state. Do not start broad future infrastructure while a gate is failing.

### Step 0 — Fixture and acceptance contract

- Select or create one redistributable English text PDF.
- Record provenance, license, checksum, expected pages, exact expected passage text, one answerable question, and one unanswerable question in the fixture manifest.
- Define deterministic expected coverage and citation results.

**Gate:** fixture rights, checksum, source spans, and expected outcomes can be reviewed without running the application.

### Step 1 — Toolchains and minimal runtime

- Scaffold the pnpm Next.js TypeScript app and uv Python package without adding unapproved production dependencies.
- Add PostgreSQL as the only M0.0 service in `compose.yaml`; bind it to local development access.
- Define validated, secret-safe settings, require complete PostgreSQL configuration when database
  access is configured, and keep committed examples placeholder-only.
- Add the smallest real formatting, linting, type-checking, and test scripts; document their actual commands only after they exist.
- Add minimal CI jobs for the checks that exist at this step. In Step 3, extend them with migration, seed, PostgreSQL integration, drift, and migration-cycle verification. Full container CI remains M0.

**Gate:** web and API start, PostgreSQL is reachable, health succeeds, and the initial checks pass from a clean checkout.

### Step 2 — Contracts, identity context, and dependency skeleton

- Define Pydantic request, result, error, and stream-event contracts.
- Expose OpenAPI and generate or mechanically verify the frontend TypeScript contract.
- Implement the fixed server-side local identity/workspace dependency.
- Establish the composition root and module import directions without empty feature implementations.

**Gate:** contract drift fails an automated check, and a client-supplied workspace cannot alter the effective workspace.

### Step 3 — Migration and scoped persistence

- Create the reviewed Alembic migration for the minimum physical schema.
- Implement transaction and SQLAlchemy session factories.
- Implement module-owned repositories that require explicit workspace context.
- Seed two deterministic sanitized workspaces through an idempotent command so negative isolation
  behavior can be tested even though the UI uses one. Repeated runs preserve identical rows, and a
  conflicting deterministic identifier aborts without overwriting existing data.

**Gate:** an empty database migrates to head; the deterministic seed is idempotent; repository tests prove transactions, same-workspace access, cross-workspace non-disclosure, and database constraints; metadata has no migration drift; and the reviewed revision downgrades to base and upgrades back to head.

### Step 4 — Secure capture and original viewing

- Implement the `ObjectStorage` port and immutable local adapter.
- Implement upload validation, checksum calculation, atomic capture, metadata persistence, and accepted response.
- Implement the authorized original route and basic PDF viewer target.

**Gate:** the correct fixture opens; invalid signature, oversized input, overwrite attempt, path traversal input, and cross-workspace access fail safely.

### Step 5 — Processing runner

- Implement `ProcessingRun` creation and visible state transitions.
- Implement the bounded parser and versioned structure-block/chunk creation.
- Implement the separate single-job runner and its independent unit of work.
- Ensure failure leaves the immutable original and canonical run state inspectable.

**Gate:** the fixture moves `CAPTURED → PARSING → LEXICAL_READY`; a parser failure becomes `FAILED`; the API remains outside the parsing execution path.

### Step 6 — Lexical retrieval

- Write versioned English search vectors and the GIN index.
- Implement normalized queries, canonical eligibility filters, ranking, stable tie-breaking, and bounded result size.
- Persist the research request, retrieval run, and selected candidates used by the answer path.

**Gate:** the known passage is retrieved deterministically, the unanswerable query stays below the evidence threshold, and no cross-workspace or inactive-version candidate is returned.

### Step 7 — Generation and evidence validation

- Implement `AnswerGenerator` and the deterministic fixture adapter.
- Generate before opening the persistence transaction, then atomically persist the terminal answer,
  statements, evidence spans, links, and validation results.
- Implement validation for workspace, active version, candidate eligibility, exact span/hash match, statement support, and coverage.
- Reject tampered or invented identifiers and make insufficient coverage explicit.
- Resolve persisted citations through an application-owned read port, rechecking the trusted
  workspace, active current document version, validated statement, and streamed answer before
  returning an exact source location and protected original-document target.

**Gate:** only accepted statements can reach an output contract; insufficient and rejected answers
contain no usable statements or citations; invalid evidence cannot be converted into a citation; and
a late persistence failure rolls back the complete answer aggregate. A usable citation resolves only
inside its owning workspace while foreign, unknown, stale, and unlinked identifiers remain
indistinguishable.

### Step 8 — Web vertical slice

- Build the English upload, processing-status, research, answer, and citation-opening interfaces.
- Consume only the generated or mechanically checked API contract.
- Render progress independently from validated answer content.
- Keep user-facing strings centralized by feature so later localization does not require changing domain or API behavior.

**Gate:** a user can complete the full M0.0 journey in the browser and open the expected original page/passage.

### Step 9 — Acceptance, measurements, and M0 handoff

- Automate the answerable, unanswerable, invalid-output, tampered-citation, upload-limit, and workspace-isolation API cases.
- Run frontend formatting, linting, type checking, and contract checks; run backend formatting, linting, type checking, unit, integration, migration, and acceptance checks.
- Perform one manual browser smoke test for the original-page opening behavior.
- Keep live-provider testing separate, opt-in, public-fixture-only, and bounded.
- Record timings, implementation effort, failure modes, retained contracts, and required M0 replacements.

**Gate:** every M0.0 exit condition in `05-delivery-plan.md` has recorded evidence, and no temporary adapter is mislabeled as production-ready.

## 8. Verification matrix

| Risk | Minimum automated proof |
|---|---|
| Workspace isolation | Repository, API, original, search, and citation requests cannot cross between the two fixture workspaces |
| File handling | Signature, size, page, path, overwrite, and timeout cases fail safely |
| Migration safety | Empty-database upgrade succeeds; migration/model drift check succeeds; runtime schema creation is absent |
| Session safety | API and runner create independent units of work; runner accepts identifiers rather than ORM objects |
| Lexical correctness | Expected chunk wins with stable ordering; ineligible chunks never enter evidence assembly |
| Answer safety | Invalid schema, unknown IDs, altered spans, wrong workspace, and inactive versions are rejected |
| Insufficient evidence | Unanswerable fixture produces no archive-derived statement or general-knowledge completion |
| Contract consistency | Frontend contract matches current OpenAPI and handles every terminal event |
| Citation usability | Accepted citation resolves to the immutable fixture and exact expected source location |

Normal verification must not require a paid or live AI provider.

## 9. Post-M0.0 replacement matrix

| M0.0 implementation | Required next action | Target stage | Retained contract or behavior |
|---|---|---|---|
| PostgreSQL-only Compose service | Expand Compose with web, API, worker, Redis, and MinIO | M0 | Database schema history and application modules |
| Immutable local `ObjectStorage` adapter | Replace with a MinIO adapter through the existing port | M0 | Object keys, checksums, immutable capture semantics, `ObjectStorage` port |
| Single-job development runner | Deliver run IDs through Redis and Celery | M0 | `ProcessingRun`, processing service, state contract, independent unit of work |
| Minimal process-level checks and CI | Add pre-commit checks, container builds, and full container CI | M0 | Existing commands, PostgreSQL checks, and regression fixtures |
| Fixed local development identity | Retain for M0 infrastructure work; add the production-shaped authentication boundary | M1 | Server-resolved workspace context and scoped repositories |
| Deterministic fixture generator | Retain as the default test adapter; add an approved provider adapter | M1 | `AnswerGenerator` port and typed result |
| PostgreSQL lexical retrieval | Retain and add dense retrieval and fusion | M1 | `LexicalSearch`, eligibility checks, and evidence assembly input |

M0 must not rename shared domain concepts, move module ownership, or bypass existing ports merely to connect the production adapters.

## 10. Implementation prerequisites

These prerequisites were satisfied before M0.0 implementation began:

- The plan and M0.0 flow comparisons had no unresolved contradiction with the compact specifications.
- The synthetic CC0 fixture and its redistribution terms were approved and frozen in `fixtures/m0_0/manifest.yaml`.
- Production dependency additions were reviewed separately and resolved through the committed lockfiles.
- No private archive data or live provider credentials were required.
- Implementation began with the bounded fixture and foundation steps and their verification gates.

## 11. M0.0 completion record

### 11.1 Accepted assumptions and limits

- The proof is local-only, English-only, and limited to a synthetic four-page text PDF.
- PostgreSQL 17 and the ignored local object directory are the only runtime stores.
- One server-resolved development identity and workspace replace authentication; the client never
  supplies authorization through a `workspace_id`.
- Processing uses a bounded single-job process without queue durability, retries, concurrency, or
  availability guarantees.
- Answer generation is deterministic. No paid or live AI provider was required or measured.
- The measurements below demonstrate deterministic correctness on the fixture. They are not load,
  capacity, cost, or production-latency benchmarks.

### 11.2 Recorded evidence

| Exit condition | Recorded evidence |
|---|---|
| Complete answerable journey | `test_complete_m0_0_journey_opens_exact_source_and_refuses_missing_evidence` captures the real multipart fixture, runs processing in a separate process, retrieves, generates, validates, and opens the exact original bytes at page 2. |
| Explicit insufficient coverage | The same acceptance test asks the frozen dividend question and receives only `answer.insufficient`, with no statement or citation event. |
| Invalid output and tampered citation | `test_api_rejects_untrusted_answer_without_exposing_usable_content` proves unsupported statements and one-character citation changes end as `research.failed`; rejected output is not persisted as usable statements or evidence. |
| Upload limit | `test_m0_0_api_rejects_pdf_above_the_configured_upload_limit` receives `413 PAYLOAD_TOO_LARGE` before any object is published. |
| Workspace and eligibility isolation | Repository and API integration tests cover unknown and foreign identifiers, composite cross-workspace constraints, inactive chunk sets, and non-current document versions. |
| Migration safety | `scripts.verify_postgres` migrates an empty PostgreSQL database to `head`, checks model drift, downgrades to `base`, upgrades to `head` again, and removes the disposable database and volume. |
| Contract consistency | CI checks FastAPI against the committed OpenAPI snapshot; frontend tests check the generated TypeScript declaration and all terminal stream events. |
| Browser usability | A manual local smoke test completed upload, processing, answerable and unanswerable research, and opening the cited original on page 2. |

### 11.3 Measurements and implementation effort

The final local verification on 2026-09-13 recorded:

| Measurement | Result | Interpretation |
|---|---:|---|
| Frozen fixture | 10,840 bytes, 4 pages | Small deterministic proof fixture, not a representative corpus |
| Backend unit suite | 337 passed in 1.51 seconds | Deterministic checks without PostgreSQL or live providers |
| PostgreSQL integration and acceptance suite | 51 passed in 2.17 seconds | Pytest-reported execution time after database startup and migration |
| Frontend deterministic tests | 13 passed in 81.7 milliseconds | Node test-runner duration; contract generation and production build excluded |
| Migration/model drift | No new upgrade operations detected | SQLAlchemy metadata matched migration `head` before and after the downgrade/upgrade cycle |
| Manual browser journey | Functionally passed | No browser latency instrumentation was present, so no latency claim is made |
| Live-provider calls and cost | Not run; zero required | Deliberately outside the deterministic M0.0 gate |

Repository commit dates place the finalized architecture plan on 2026-09-03 and this acceptance
closure on 2026-09-13, a ten-calendar-day delivery span. This is not a person-hour measurement:
continuous work time, review time, and waiting time were not instrumented. The auditable effort unit
is therefore the ten gated implementation steps and their focused commits rather than an invented
hour estimate.

### 11.4 Observed failure modes and lessons

| Observed failure | Cause and resolution | Required follow-through |
|---|---|---|
| Upload returned HTTP 500 against an older local database | The application code expected migration `0002`, while the database remained at `0001` with the removed `original_artifacts.page_count` requirement. Upgrading to Alembic `head` restored schema/code agreement. | M0 startup and container workflows must apply migrations before serving traffic and expose migration state clearly. |
| PostgreSQL rejected a changed local password | The existing Docker volume retained credentials from its first initialization; changing environment input does not rotate an existing PostgreSQL role password. | Document safe development reset and explicit credential-rotation paths; never imply that restarting a persisted volume reinitializes credentials. |
| A fixture command failed from the wrong working directory | Its relative path resolved outside `fixtures/`. The documented commands now state their required root or `backend/` context. | Container entrypoints and M0 task commands must use explicit working directories and validated paths. |
| Invalid generated evidence reached validation | The validator rejected unsupported text and tampered exact spans before emitting or persisting usable evidence. | Keep the validator as a mandatory boundary when the deterministic adapter is replaced by a live provider. |
| Next.js build could not bind an internal port inside a restricted development sandbox | Turbopack required a local process/port capability unavailable in that sandbox; the same production build passed in the normal local environment. | Keep CI as the environment-neutral build gate; do not weaken application security settings to accommodate a tooling sandbox. |

### 11.5 Retained M0 contracts

M0 should extend the proof by replacing adapters and strengthening operations, not by rebuilding its
domain path. Retain:

- the `Identity`, `Documents`, `Processing`, `Search`, `Research`, and `Evidence` module ownership and
  dependency direction;
- `WorkspaceContext`, workspace-scoped repositories, and indistinguishable foreign/unknown lookup
  behavior;
- immutable document versions, original checksums and object keys, `ProcessingRun`, structure blocks,
  chunk sets, chunks, research requests, answers, statements, evidence spans, and validations;
- application-owned `ObjectStorage`, processing, lexical search, evidence source, and
  `AnswerGenerator` boundaries;
- Pydantic HTTP and event contracts, the committed OpenAPI snapshot, and generated TypeScript types;
- Alembic history, caller-owned transaction boundaries, server-side evidence validation, and the
  deterministic acceptance fixture and regression tests.

The M0 replacements listed in section 9 must run the same acceptance cases through MinIO,
Redis/Celery, and the production-shaped container boundaries. Until that succeeds, M0.0 remains a
validated product proof rather than a production-ready foundation.
