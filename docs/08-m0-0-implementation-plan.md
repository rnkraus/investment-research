# Investment Research Archive — M0.0 Implementation Plan

**Status:** Ready for implementation v0.1
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
12. **Immutable local originals:** the M0.0 storage adapter writes under a configured, Git-ignored storage root outside the web root, uses server-generated keys and atomic writes, verifies checksums, and never overwrites an existing object.

These decisions are implementation rules for M0.0. Only decisions that remain cross-cutting and expensive to reverse are promoted to ADRs after the proof provides evidence.

## 3. Repository structure

Create files only when their implementation step begins; do not scaffold empty placeholder modules.

```text
.
├── AGENTS.md
├── README.md
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
│   │   └── export_openapi.py            # generate or verify the committed API contract
│   ├── alembic.ini
│   ├── migrations/
│   │   ├── env.py
│   │   └── versions/
│   ├── src/investment_research/
│   │   ├── main.py                       # FastAPI composition root entry point
│   │   ├── bootstrap.py                  # dependency wiring only
│   │   ├── settings.py                   # validated non-secret settings
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
│   │       ├── database/
│   │       ├── object_storage/
│   │       ├── parsing/
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
- Define validated non-secret settings and placeholder-only example configuration if configuration requires it.
- Add the smallest real formatting, linting, type-checking, and test scripts; document their actual commands only after they exist.
- Add minimal CI jobs for the checks that exist at this step. Extend them with migration verification when Step 3 introduces Alembic and the first migration. Full container CI remains M0.

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

**Gate:** an empty database migrates to head; repository tests prove same-workspace access and cross-workspace non-disclosure.

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
- Persist the typed answer, statements, evidence spans, links, and validation results.
- Implement validation for workspace, active version, candidate eligibility, exact span/hash match, statement support, and coverage.
- Reject tampered or invented identifiers and make insufficient coverage explicit.

**Gate:** only accepted statements can reach an output contract; invalid evidence cannot be converted into a citation.

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

## 9. M0 replacement matrix

| M0.0 implementation | M0 action | Retained contract or behavior |
|---|---|---|
| PostgreSQL-only Compose service | Expand Compose with web, API, worker, Redis, and MinIO | Database schema history and application modules |
| Immutable local `ObjectStorage` adapter | Replace with MinIO adapter | Object keys, checksums, immutable capture semantics, port |
| Single-job development runner | Deliver run IDs through Redis/Celery | `ProcessingRun`, processing service, state contract, independent unit of work |
| Fixed local development identity | Add production-shaped authentication boundary later in M1 | Server-resolved workspace context and scoped repositories |
| Deterministic fixture generator | Retain for tests and add approved provider adapter | `AnswerGenerator` and typed result |
| PostgreSQL lexical retrieval | Retain and later add dense retrieval | `LexicalSearch`, eligibility checks, evidence assembly input |
| Minimal checks and CI | Add pre-commit and full container/integration CI | Existing commands and regression fixtures |

M0 must not rename shared domain concepts, move module ownership, or bypass existing ports merely to connect the production adapters.

## 10. Ready-to-start checklist

Implementation may begin when:

- this plan and the M0.0 flow comparisons have no unresolved contradiction with the compact specifications;
- the fixture choice and redistribution terms are approved;
- any new production dependency beyond the fixed technical baseline is separately explained and approved;
- no private archive data or credentials are required;
- the first implementation change is limited to Step 0 or Step 1 and includes its corresponding verification.
