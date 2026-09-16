# Investment Research Archive — Delivery Plan

**Status:** Active v0.8 — M0 complete; local single-user M1 planning active
**Approach:** Lean proof of value, then local-first vertical slices, evidence before agents

**Related:** [01-product-scope.md](./01-product-scope.md), [02-system-design.md](./02-system-design.md), [03-domain-model.md](./03-domain-model.md), [04-security-and-operations.md](./04-security-and-operations.md), [07-m0-0-architecture-flows.md](./07-m0-0-architecture-flows.md), [08-m0-0-implementation-plan.md](./08-m0-0-implementation-plan.md), [09-m0-implementation-plan.md](./09-m0-implementation-plan.md), [10-m1-implementation-plan.md](./10-m1-implementation-plan.md)

## 1. Delivery strategy

Build the smallest complete research path first:

```text
Import → Process → Search → Answer → Validate → Open citation
```

Then improve reliability, retrieval quality, user experience, and operations around that path.

Working rules:

- prove the core product and evidence contract in M0.0 before building the production-shaped local stack;
- deliver end-to-end behavior instead of completing one technical layer at a time;
- keep M0.0 local-only and, from M0 onward, keep the application in Docker Compose until the core journey is reliable;
- use a small public fixture corpus before a real private archive exists;
- separate deterministic correctness tests from corpus-quality evaluation, and establish a small representative baseline before retrieval choices become release candidates;
- version prompts, models, parsers, chunks, embeddings, and retrieval configuration from the start;
- add an ADR only when a significant decision is made, not for routine implementation choices;
- do not add LangGraph until direct RAG has stable traces and a measured limitation.

## 2. Definition of done

M0.0 is governed by its narrower proof-specific constraints and exit gate. For M0 through M9, a milestone is complete only when:

- its user-visible path works end to end;
- important failure and retry behavior is implemented;
- authorization and workspace boundaries are tested;
- migrations and configuration are reproducible;
- logs, traces, and useful error states exist;
- relevant unit, integration, and acceptance tests pass;
- documentation and ADRs match the implemented behavior;
- no known critical security or evidence-integrity issue remains.

## 3. Milestone overview

| Milestone | Outcome | Main exit gate |
|---|---|---|
| M0.0 — Lean proof of value | The core archive promise is demonstrated with minimal local infrastructure | One known question produces a validated answer whose citation opens the exact fixture location |
| M0 — Engineering foundation | Reproducible local development and basic delivery checks | One command starts the stack and automated checks pass |
| M1 — Thin vertical slice | One document can produce one validated cited answer | Citation opens the exact original location |
| M2 — Reliable ingestion | Imports, retries, versions, reprocessing, and deletion are consistent | Failure and stale-index scenarios preserve canonical state |
| M3 — Archive and discovery | Documents and companies can be browsed, filtered, and searched | Exact, lexical, and semantic discovery work together |
| M4 — Production-shaped direct RAG | Routed hybrid retrieval, reranking, conversation, and evidence rules work | Critical correctness cases and the initial representative quality baseline pass |
| M5 — Local release hardening | Security, recovery, observability, and cost controls are exercised | All local release gates pass |
| M6 — Public portfolio demo | Sanitized read-only demo is safely hosted | Public-release security, operations, and pre-demo quality gates pass |
| M7 — Expanded corpus evaluation | Retrieval and answer changes are compared on broader real use cases | Baselines and regression gates are evidence-based |
| M8 — Structured research knowledge | Claims, topics, profiles, and impact suggestions become reviewable | Suggestions remain evidence-linked and human-controlled |
| M9 — Bounded agentic research | LangGraph handles justified multi-step cases | It measurably improves named cases within budgets |

Milestones are sequential release gates, not fixed-duration sprints. Small refactors and documentation updates happen continuously.

## 4. M0.0 — Lean proof of value

M0.0 was locally accepted on 2026-09-13 as a deliberately constrained, local-only walking
skeleton. It validates the product's highest-risk promise before the full production-shaped runtime
is assembled. It is not a releasable V1 environment and does not replace any M0–M9 gate.

The target-to-M0.0 module and workflow mappings are maintained in [07-m0-0-architecture-flows.md](./07-m0-0-architecture-flows.md). The exact repository structure, contracts, implementation order, and verification gates are maintained in [08-m0-0-implementation-plan.md](./08-m0-0-implementation-plan.md).

### Build

1. Freeze the redistributable English PDF fixture, its checksum, the answerable and unanswerable questions, and their exact expected page and passage citations.
2. Scaffold the minimum pnpm/Next.js and uv/FastAPI applications, PostgreSQL development service, automated checks, and documented local commands.
3. Establish Alembic immediately and migrate the minimum canonical evidence spine rather than creating tables from application startup code.
4. Establish the Pydantic/OpenAPI HTTP and validated-stream contracts and derive the frontend TypeScript contract from them.
5. Resolve a fixed local development identity and workspace on the server, and enforce the workspace in every repository, storage, retrieval, and citation operation.
6. Capture the fixture through the production `ObjectStorage` port backed by immutable development-only local storage.
7. Create a canonical `ProcessingRun` and execute it through a separate, bounded, single-job development runner without Celery or Redis.
8. Parse structure and pages, create versioned chunks, and write a versioned PostgreSQL `tsvector` with a GIN index.
9. Run deterministic PostgreSQL lexical retrieval for the known answerable and deliberately unanswerable questions.
10. Generate a typed statement-and-evidence response through an `AnswerGenerator` port, using the deterministic adapter by default and an approved provider adapter only as an opt-in public-fixture smoke test.
11. Apply the real server-side evidence validator before emitting statement or citation events, then open the immutable original at the cited page and passage.
12. Automate the acceptance cases and record assumptions, measured effort, observed failure modes, and the exact adapter replacements required by M0.

### Constraints

- Use the repository-owned frozen synthetic PDF as the only accepted M0.0 processing and research
  fixture. Technically parsing another file does not place it inside the approved proof boundary.
- Bind services to local development access only; no public deployment or private archive data is allowed.
- Do not add Pinecone, Redis, Celery, SeaweedFS, LangSmith, reranking, conversation memory,
  lifecycle workflows, or future-feature modules.
- Treat M0.0 as a subset of the target system: reuse the production module, entity, state, and workflow names whenever their meaning is the same.
- Keep final application-owned contracts, module directions, and migrations; mark only the local storage adapter, local processing runner, and deterministic fixture generator as M0.0 implementations.
- Treat the bounded development runner as process and resource separation, not as secret,
  filesystem, or network isolation; [ADR-0004](./adr/0004-m0-0-parser-isolation-boundary.md)
  governs this exception and its mandatory M0 removal.
- Return accepted upload metadata and processing status from the API; never perform PDF parsing on the API event loop or share SQLAlchemy sessions across requests and runner executions.
- Emit progress before validation if useful, but emit no answer statement or citation until the complete typed result passes server-side validation.
- Use the smallest physical schema that preserves the selected domain semantics; do not create empty modules or tables solely because the conceptual V1 model contains them.
- A shortcut cannot become production behavior merely because the proof succeeds.

### Exit gate

- The answerable fixture produces only validated archive-derived statements and opens the exact expected source location.
- The unanswerable fixture returns explicit insufficient coverage without filling the gap from general model knowledge.
- A tampered evidence identifier, span, inactive version, or cross-workspace lookup is rejected server-side.
- A fresh PostgreSQL database can be migrated to the M0.0 head and the application does not create or alter schema at runtime.
- Formatting, linting, type checking, backend unit and integration tests, frontend contract checks, and the API acceptance path pass through documented commands.
- The proof identifies which contracts can be retained and which temporary implementations M0 must replace.
- No M0.0-only storage or execution shortcut is treated as satisfying an M0 or M1 reliability gate.

## 5. M0 — Engineering foundation

The exact M0 runtime topology, retained contracts, adapter-replacement order, work blocks, and
verification matrix are maintained in
[09-m0-implementation-plan.md](./09-m0-implementation-plan.md).

### Build

- Retain proven M0.0 contracts where they preserve the target architecture and replace all development-only storage and execution shortcuts.
- Extend the M0.0 repository structure for the production worker process, operational adapters, and broader tests without moving the established application modules.
- Add Dockerfiles and Docker Compose for Next.js, FastAPI, Celery, PostgreSQL, Redis, and SeaweedFS.
- Keep object-storage access provider-neutral in application code and apply the local-versus-public
  boundary from [ADR-0007](./adr/0007-local-s3-provider.md).
- Establish and test a least-privilege parser boundary that receives no application or provider
  credentials, sees only bounded parse input and output, and cannot use unnecessary network access.
- Add configuration validation, ignored local secrets, health checks, and structured logging.
- Continue the M0.0 Alembic history and add a repeatable local reset/seed workflow that cannot target broad filesystem paths.
- Extend the M0.0 formatting, linting, type checking, and test commands with pre-commit checks and full container CI.
- Retain the M0.0 object-storage and generation ports and add application-owned interfaces for vector retrieval, embeddings, reranking, and tracing.

### Exit gate

- A new developer can start the complete local stack from documented steps.
- Web, API, worker, database, queue, and object storage health checks pass.
- Parser-isolation tests prove secret non-disclosure, restricted filesystem access, blocked
  unnecessary network access, enforced resource limits, and a safe canonical failure state.
- CI builds the containers and runs the initial automated checks.
- Secrets and generated data are not committed.
- The M0.0 acceptance cases still pass through the production-shaped local service boundaries.

## 6. M1 — Thin vertical slice

The first complete RAG slice hardens the M0.0 product proof and intentionally supports one local
single-user document path before broad feature coverage. M1 retains the server-owned fixed local
identity and loopback-only boundary so implementation can focus on retrieval, generation, and
evidence. It is not an authenticated or publicly deployable release.

The exact M1 decisions, repository delta, backend-to-CI-to-frontend sequence, and verification
matrix are maintained in [10-m1-implementation-plan.md](./10-m1-implementation-plan.md).

### Build

1. Retain one fixed server-owned local owner context and publish the host entry point only on
   loopback.
2. Upload one text PDF or submit pasted text.
3. Store the immutable original through the S3-compatible adapter and canonical metadata in
   PostgreSQL; SeaweedFS supplies the local object store.
4. Enqueue and run parsing, structure-block creation, chunking, embedding, and indexing.
5. Display document processing state.
6. Run lexical and dense retrieval for one archive question.
7. Generate a typed answer with statement-level evidence identifiers.
8. Validate the evidence server-side.
9. Stream validated output and open the original at the cited page or passage.
10. Record the request, versions, evidence, latency, usage, and trace ID.

### Exit gate

Use a tiny redistributable fixture set containing at least:

- one English PDF document;
- one English pasted-text document;
- one answerable question;
- one deliberately unanswerable question;
- one citation with a known page and passage.

The answerable case must cite the correct original location. The unanswerable case must not be completed from general model knowledge.

## 7. M2 — Reliable ingestion and lifecycle

### Build

- Multiple PDF upload and pasted-text versioning.
- File and resource limits, safe parser failures, and visible retry controls.
- Exact duplicate detection with preserved provenance.
- Idempotent processing stages with bounded retry policies.
- Versioned parser, structure, chunk, embedding, and index outputs.
- Transactional outbox, deterministic vector IDs, index receipts, and reconciliation.
- Reprocessing with atomic activation of new derived versions.
- Soft deletion, 30-day restoration semantics, and purge workflow.
- Tests for duplicate messages, provider timeouts, partial indexing, stale vectors, deletion, and restoration.

### Exit gate

Replaying any job or outbox event produces the same final state. Deleted or inactive content cannot become evidence even when stale vectors still exist.

## 8. M3 — Archive and discovery

### Build

- Document list, detail, version, processing-status, and original-document views.
- Basic companies, aliases, exchange-specific tickers, and watchlist.
- Manual and reviewable suggested document–company assignments.
- Filters for company, ticker, document type, publication date, import date, and state.
- Exact identifier and phrase lookup using PostgreSQL.
- Lexical passage search and Pinecone dense search.
- Search-result explanations showing route, filters, document, page, and passage.
- Initial versioned evaluation core using public or sanitized representative document forms and reviewed questions with expected evidence and failure categories.

### Exit gate

The fixture corpus can be found through exact ticker lookup, lexical wording, semantic paraphrase, metadata filtering, and document navigation without using chat. Before M4 begins, the initial evaluation core and its critical metrics and thresholds are frozen so later retrieval and answer comparisons use the same versioned cases.

## 9. M4 — Production-shaped direct RAG

### Build

- Typed query context with workspace, company, document, and temporal filters.
- Routing between structured lookup, hybrid research, and their explicit combination.
- PostgreSQL lexical candidates plus Pinecone dense candidates.
- Unweighted RRF followed by bounded multilingual cross-encoder reranking.
- Context assembly with deterministic eligibility and token budgets.
- Conversation follow-ups with explicit retained context.
- Statement-level answer generation and server-side evidence validation.
- Complete, partial, insufficient, and conflicting coverage behavior.
- Public-knowledge-time and personal-archive-time modes.
- OpenTelemetry/OpenInference traces and LangSmith inspection.
- Per-operation latency, token, and estimated-cost recording.
- Initial representative measurements that separate retrieval recall, reranking, answer support, citation correctness, refusal of unanswerable questions, temporal behavior, latency, and cost.

### Exit gate

Approved fixture cases demonstrate:

- valid citations for every archive-derived statement;
- rejection of fabricated, stale, deleted, cross-workspace, or time-ineligible evidence;
- explicit insufficient coverage;
- visible conflicting evidence;
- correct follow-up context without treating conversation history as evidence;
- different results for public and personal archive time where expected.

The initial representative evaluation baseline is recorded against one versioned dataset and configuration manifest, and every agreed critical threshold passes. This early baseline is a release gate and a regression signal, not a claim of statistical completeness.

## 10. M5 — Local release hardening

### Build and exercise

- Select and document the OIDC provider, client library, server-side session boundary, and local
  test issuer before adding authentication dependencies.
- Add canonical users and workspace memberships, authenticate the private owner, provision the
  isolated read-only demo capability, and replace the fixed local identity outside tests.
- Cross-workspace tests at API, repository, object, queue, vector, citation, and trace boundaries.
- Malicious-file, prompt-injection, invalid-schema, and fabricated-citation fixtures.
- Dependency, secret, and container scanning.
- Backup and restore of PostgreSQL and original objects.
- Full vector-index rebuild from canonical active chunks.
- Reconciliation, stuck-job, provider-outage, and rate-limit exercises.
- Deletion and recovery-period tests.
- Useful local dashboards or reports for imports, research requests, errors, and cost.
- Short operational runbooks from `04-security-and-operations.md`.

### Exit gate

Every local release gate in `04-security-and-operations.md` passes. The complete authenticated
product journey can be demonstrated from a clean environment without manual database repair, and
the fixed local identity remains available only to explicit deterministic tests.

## 11. M6 — Public portfolio demo

Do not begin this milestone until the local product is stable.

### Decide

- Select a simple container-capable host based on cost, EU-region availability, managed data services, operational effort, and portfolio value.
- Choose whether a simpler platform or AWS provides the better learning-to-complexity trade-off at that time.
- Record the decision and alternatives in an ADR.

### Build

- Provision isolated application, worker, PostgreSQL, object-storage, queue, vector, and provider environments.
- Deploy the accepted managed-identity integration plus HTTPS, secret storage, backups, monitoring,
  alerts, and spending limits.
- Load only the sanitized demo corpus.
- Enforce read-only demo behavior and rate limits.
- Automate deployment, migrations, smoke tests, and rollback.
- Publish a concise architecture explanation, evaluation summary, and limitations.

### Exit gate

All public-demo release gates in `04-security-and-operations.md` pass, including restore, rollback, and the versioned pre-demo quality evaluation. No private archive content or credential is present in the demo environment.

## 12. M7 — Expanded evaluation with a representative corpus

M7 expands the pre-demo M3–M4 evaluation core with broader representative documents, questions, failure modes, and repeated comparisons. Deterministic fixtures remain separate correctness tests; the initial representative baseline already gates M4 and M6.

### Build

- Expand the versioned evaluation datasets with additional questions, scopes, expected evidence, acceptable answer properties, and failure categories.
- Separate retrieval, reranking, answer, citation, routing, temporal, latency, and cost measurements.
- Revalidate the direct-RAG baseline before adopting a major retrieval-method change.
- Use LangSmith experiments and application-owned result records; use RAGAS metrics selectively where they match the evaluation question.
- Compare changes on the same dataset and configuration manifest.
- Promote reliable failure cases from production traces into the regression set after privacy review.

### Expanded comparisons

- lexical-only versus dense-only versus hybrid retrieval;
- RRF with and without multilingual reranking;
- chunking and embedding versions;
- PostgreSQL lexical ranking versus true BM25 only if the corpus exposes a gap;
- context size, model, prompt, latency, and cost trade-offs.

### Exit gate

The chosen baseline has documented quality, latency, and cost. High-risk retrieval or generation changes cannot be released when they regress agreed critical metrics beyond the accepted tolerance.

## 13. M8 — Structured research knowledge

Add one evidence-backed capability at a time:

1. actors, aliases, roles, and source attribution;
2. claim occurrences and classification;
3. metrics and forecasts;
4. reviewed support, contradiction, update, and supersession relations;
5. topics and company–topic relations;
6. versioned company-profile facts;
7. watchlist impact suggestions;
8. personalized ranking and coverage findings.

Each increment requires typed extraction, provenance, confidence, review state, correction behavior, and evaluation examples. Model output remains a suggestion until explicitly accepted where acceptance is required.

## 14. M9 — Bounded agentic research

Introduce LangGraph only when:

- direct RAG is stable and measured;
- at least one named multi-step user case cannot be handled well by the direct route;
- the required tools already exist as independently tested application services;
- expected quality benefit justifies added latency, cost, and risk.

Start with one graph that plans a bounded set of archive searches, checks evidence gaps, and either synthesizes a validated answer or stops with a partial result.

Require tool allowlists, workspace enforcement, step/time/token/cost budgets, checkpoints, termination tests, traces, and agent-specific evaluation. The graph uses the existing retrieval and evidence services; it does not create a parallel RAG implementation.

## 15. Immediate next actions

1. Complete the remaining M1 API, processing, indexing, retrieval, usage, and trace contract freeze
   before changing dependencies or creating provider resources.
2. Implement pasted-text capture and the accepted stable source locators against the frozen
   synthetic PDF and pasted-text fixture set.
3. Preserve the fixed local owner context and loopback-only boundary without treating a
   client-supplied workspace identifier as authorization.
4. Implement and evaluate M1 through the retained identity, storage, processing, search, research,
   evidence, and operations contracts.
5. Continue sequentially through M9 and update this plan after each milestone using measured results
   and newly available data.
