# Backend Tests

This directory verifies deterministic application behavior and architectural boundaries without live paid providers.

## Structure

| Path           | Responsibility                                                                                    |
| -------------- | ------------------------------------------------------------------------------------------------- |
| `unit/`        | Fast tests for settings, health, identity, contracts, OpenAPI drift, and module dependency rules. |
| `integration/` | Opt-in PostgreSQL tests for migrations, seeds, persistence, constraints, and isolation.           |
| `acceptance/`  | Complete fixture journeys through explicitly activated disposable runtimes.                     |

## Current coverage

```mermaid
flowchart TD
    TESTS[Backend test suite] --> RUNTIME[Settings and health]
    TESTS --> IDENTITY[Workspace isolation boundary]
    TESTS --> CONTRACTS[API and stream validation]
    TESTS --> OPENAPI[Committed contract drift]
    TESTS --> ARCH[Module import rules]
    TESTS --> POSTGRES[Opt-in PostgreSQL integration]
    TESTS --> ACCEPTANCE[Opt-in complete M0 runtime]
    POSTGRES --> SEED[Deterministic workspace seed]
    POSTGRES --> TX[Commit and rollback]
    POSTGRES --> DOCS[Document workspace isolation]
    POSTGRES --> CONSTRAINTS[Workspace and value constraints]
    POSTGRES --> IMMUTABLE[Immutable versions and originals]
    POSTGRES --> PROCESSING[Processing lifecycle and lexical output]
    POSTGRES --> SEARCH[Lexical query eligibility, ranking, and persistence]
    POSTGRES --> RESEARCH[Answer lifecycle and statement ordering]
    POSTGRES --> EVIDENCE[Evidence chain and validation integrity]
    POSTGRES --> ANSWER[Atomic validated-answer aggregate]
    ACCEPTANCE --> AUTO[Automatic Celery processing]
    AUTO --> CITATION[Validated answer and exact original evidence]
    CITATION --> REPEAT[Repeated answer reuses canonical evidence]
    REPEAT --> SAFELOGS[Runtime logs exclude sensitive sentinels]
```

Tests use application factories and explicit test settings. They must not read secret environment files, depend on private documents, contact paid providers, or weaken workspace and evidence rules merely to pass.

The M0 acceptance test is inactive during normal unit and PostgreSQL suites. The
`scripts.verify_m0_runtime` command activates it only inside a non-root, read-only verification
container connected to a disposable migrated Compose runtime. It proves automatic Celery processing,
the validated answer and citation path, repeated-request reuse of the canonical evidence span,
byte-identical original access, and explicit insufficient evidence without invoking the M0.0 manual
runner. The outer verifier additionally captures web, API, worker, and parser logs and fails if they
contain its injected credential marker, any exact generated credential, or the fixture evidence text.

PostgreSQL integration tests are skipped unless `INVESTMENT_RESEARCH_DATABASE_URL` is explicitly
configured. They additionally require `INVESTMENT_RESEARCH_ENVIRONMENT=test`, use a schema already
migrated by Alembic, and never create tables implicitly. The current suite verifies durable commits,
complete rollback after failure, and non-disclosure of documents, current versions, and original
artifact metadata across workspace boundaries. It also verifies database rejection of cross-workspace
document relationships and invalid constrained values, plus trigger-enforced immutability for document
versions and original artifacts. Processing coverage verifies the captured-to-lexical-ready lifecycle,
rejects cross-workspace source versions, enforces one active chunk set per profile, and confirms that
PostgreSQL builds searchable English `tsvector` values from chunk text. Processing-status API
coverage additionally proves that an owning workspace can read a run while foreign and unknown run
identifiers produce the same response. Search coverage verifies PostgreSQL full-text matching,
eligibility-first workspace, current-version, active-set, configuration, document, and temporal
filters, deterministic score ordering and limiting, stable candidate persistence, and database
rejection of foreign request and chunk relationships and duplicate ranks within one retrieval
origin. Research coverage follows every permitted transition from `REQUESTED` to `STREAMED`, verifies
ordered statements and non-disclosure, and rejects foreign or mismatched request-to-retrieval
relationships and duplicate versions. Retrieval orchestration coverage verifies exact-question
preservation, effective temporal filters, hit-to-candidate alignment, one atomic commit for request,
run, and candidates, and complete rollback after a post-search persistence failure. Evidence coverage
verifies atomically persisted exact spans, statement relationships, and answer validations; stable
scoped reads; cross-workspace source and subject rejection; and unique answer- and statement-level
rule results. Generation coverage proves that the untrusted generator runs outside the transaction,
only canonical validated statements become visible output, and insufficient or rejected results
persist without usable content. The PostgreSQL orchestration tests verify the complete answer,
statement, span, relationship, and validation round trip plus rollback of every staged record when a
late validation write fails. A composition-root integration case additionally calls the complete
`AnswerResearchRequestService` created by `build_container()` and proves that the pinned M0.0
processing, retrieval, generation, support, and validation profiles produce the manifest answer and
exact page-two span together. Research-route unit tests verify safe SSE mapping, terminal event
shapes, request translation, and failure redaction. PostgreSQL API tests exercise the answerable and
unanswerable manifest questions through the real container and prove that foreign document filters
remain indistinguishable from unknown identifiers without emitting answer content.

For a complete local check, `uv run --locked python -m scripts.verify_postgres` creates a uniquely
named disposable Docker environment, migrates and seeds it, runs this integration suite, verifies
migration drift and the downgrade/upgrade cycle, and removes its container and volume even after a
failed check.

Run commands are documented in the repository [`README.md`](../../README.md#verify-changes). Database
tests must prove both same-workspace access and cross-workspace non-disclosure.
