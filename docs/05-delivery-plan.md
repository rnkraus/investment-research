# Investment Research Archive — Delivery Plan

**Status:** Draft v0.1  
**Approach:** Local-first, vertical slices, evidence before agents  
**Related:** [01-product-scope.md](./01-product-scope.md), [02-system-design.md](./02-system-design.md), [03-domain-model.md](./03-domain-model.md), [04-security-and-operations.md](./04-security-and-operations.md)

## 1. Delivery strategy

Build the smallest complete research path first:

```text
Import → Process → Search → Answer → Validate → Open citation
```

Then improve reliability, retrieval quality, user experience, and operations around that path.

Working rules:

- deliver end-to-end behavior instead of completing one technical layer at a time;
- keep the application local in Docker Compose until the core journey is reliable;
- use a small public fixture corpus before a real private archive exists;
- separate deterministic correctness tests from later corpus-quality evaluation;
- version prompts, models, parsers, chunks, embeddings, and retrieval configuration from the start;
- add an ADR only when a significant decision is made, not for routine implementation choices;
- do not add LangGraph until direct RAG has stable traces and a measured limitation.

## 2. Definition of done

A milestone is complete only when:

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
| M0 — Engineering foundation | Reproducible local development and basic delivery checks | One command starts the stack and automated checks pass |
| M1 — Thin vertical slice | One document can produce one validated cited answer | Citation opens the exact original location |
| M2 — Reliable ingestion | Imports, retries, versions, reprocessing, and deletion are consistent | Failure and stale-index scenarios preserve canonical state |
| M3 — Archive and discovery | Documents and companies can be browsed, filtered, and searched | Exact, lexical, and semantic discovery work together |
| M4 — Production-shaped direct RAG | Routed hybrid retrieval, reranking, conversation, and evidence rules work | Missing, conflicting, and temporal cases pass |
| M5 — Local release hardening | Security, recovery, observability, and cost controls are exercised | All local release gates pass |
| M6 — Public portfolio demo | Sanitized read-only demo is safely hosted | Public-release security and operations gates pass |
| M7 — Corpus evaluation | Retrieval and answer changes are compared on real use cases | Baselines and regression gates are evidence-based |
| M8 — Structured research knowledge | Claims, topics, profiles, and impact suggestions become reviewable | Suggestions remain evidence-linked and human-controlled |
| M9 — Bounded agentic research | LangGraph handles justified multi-step cases | It measurably improves named cases within budgets |

Milestones are sequential release gates, not fixed-duration sprints. Small refactors and documentation updates happen continuously.

## 4. M0 — Engineering foundation

### Build

- Establish the repository structure for web, API, workers, shared contracts, migrations, tests, and documentation.
- Add Dockerfiles and Docker Compose for Next.js, FastAPI, Celery, PostgreSQL, Redis, and MinIO.
- Add configuration validation, ignored local secrets, health checks, and structured logging.
- Establish database migrations and a repeatable local reset/seed workflow that cannot target broad filesystem paths.
- Add formatting, linting, type checking, unit-test commands, pre-commit checks, and CI.
- Define application-owned interfaces for object storage, vector retrieval, embeddings, reranking, generation, and tracing.

### Exit gate

- A new developer can start the complete local stack from documented steps.
- Web, API, worker, database, queue, and object storage health checks pass.
- CI builds the containers and runs the initial automated checks.
- Secrets and generated data are not committed.

## 5. M1 — Thin vertical slice

The first slice intentionally supports one simple document path before broad feature coverage.

### Build

1. Authenticate the owner and create private and demo workspace fixtures.
2. Upload one text PDF or submit pasted text.
3. Store the immutable original in MinIO and canonical metadata in PostgreSQL.
4. Enqueue and run parsing, structure-block creation, chunking, embedding, and indexing.
5. Display document processing state.
6. Run lexical and dense retrieval for one archive question.
7. Generate a typed answer with statement-level evidence identifiers.
8. Validate the evidence server-side.
9. Stream validated output and open the original at the cited page or passage.
10. Record the request, versions, evidence, latency, usage, and trace ID.

### Exit gate

Use a tiny redistributable fixture set containing at least:

- one German document;
- one English document;
- one answerable question;
- one deliberately unanswerable question;
- one citation with a known page and passage.

The answerable case must cite the correct original location. The unanswerable case must not be completed from general model knowledge.

## 6. M2 — Reliable ingestion and lifecycle

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

## 7. M3 — Archive and discovery

### Build

- Document list, detail, version, processing-status, and original-document views.
- Basic companies, aliases, exchange-specific tickers, and watchlist.
- Manual and reviewable suggested document–company assignments.
- Filters for company, ticker, document type, publication date, import date, and state.
- Exact identifier and phrase lookup using PostgreSQL.
- Lexical passage search and Pinecone dense search.
- Search-result explanations showing route, filters, document, page, and passage.

### Exit gate

The fixture corpus can be found through exact ticker lookup, lexical wording, semantic paraphrase, metadata filtering, and document navigation without using chat.

## 8. M4 — Production-shaped direct RAG

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

### Exit gate

Approved fixture cases demonstrate:

- valid citations for every archive-derived statement;
- rejection of fabricated, stale, deleted, cross-workspace, or time-ineligible evidence;
- explicit insufficient coverage;
- visible conflicting evidence;
- correct follow-up context without treating conversation history as evidence;
- different results for public and personal archive time where expected.

## 9. M5 — Local release hardening

### Build and exercise

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

Every local release gate in `04-security-and-operations.md` passes. The complete product journey can be demonstrated from a clean environment without manual database repair.

## 10. M6 — Public portfolio demo

Do not begin this milestone until the local product is stable.

### Decide

- Select a simple container-capable host based on cost, EU-region availability, managed data services, operational effort, and portfolio value.
- Choose whether a simpler platform or AWS provides the better learning-to-complexity trade-off at that time.
- Record the decision and alternatives in an ADR.

### Build

- Provision isolated application, worker, PostgreSQL, object-storage, queue, vector, and provider environments.
- Add managed identity, HTTPS, secret storage, backups, monitoring, alerts, and spending limits.
- Load only the sanitized demo corpus.
- Enforce read-only demo behavior and rate limits.
- Automate deployment, migrations, smoke tests, and rollback.
- Publish a concise architecture explanation, evaluation summary, and limitations.

### Exit gate

All public-demo release gates in `04-security-and-operations.md` pass, including restore and rollback exercises. No private archive content or credential is present in the demo environment.

## 11. M7 — Evaluation with a representative corpus

Full quality evaluation begins when representative documents and real research questions exist. Earlier milestones still use deterministic fixtures and regression tests.

### Build

- Create versioned evaluation datasets containing question, scope, expected evidence, acceptable answer properties, and failure category.
- Separate retrieval, reranking, answer, citation, routing, temporal, latency, and cost measurements.
- Establish the direct-RAG baseline before changing retrieval methods.
- Use LangSmith experiments and application-owned result records; use RAGAS metrics selectively where they match the evaluation question.
- Compare changes on the same dataset and configuration manifest.
- Promote reliable failure cases from production traces into the regression set after privacy review.

### Initial comparisons

- lexical-only versus dense-only versus hybrid retrieval;
- RRF with and without multilingual reranking;
- chunking and embedding versions;
- PostgreSQL lexical ranking versus true BM25 only if the corpus exposes a gap;
- context size, model, prompt, latency, and cost trade-offs.

### Exit gate

The chosen baseline has documented quality, latency, and cost. High-risk retrieval or generation changes cannot be released when they regress agreed critical metrics beyond the accepted tolerance.

## 12. M8 — Structured research knowledge

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

## 13. M9 — Bounded agentic research

Introduce LangGraph only when:

- direct RAG is stable and measured;
- at least one named multi-step user case cannot be handled well by the direct route;
- the required tools already exist as independently tested application services;
- expected quality benefit justifies added latency, cost, and risk.

Start with one graph that plans a bounded set of archive searches, checks evidence gaps, and either synthesizes a validated answer or stops with a partial result.

Require tool allowlists, workspace enforcement, step/time/token/cost budgets, checkpoints, termination tests, traces, and agent-specific evaluation. The graph uses the existing retrieval and evidence services; it does not create a parallel RAG implementation.

## 14. Immediate next actions

1. Review and accept the five compact design documents.
2. Create only the ADRs needed before implementation starts.
3. Define the M1 acceptance fixtures and exact expected citations.
4. Scaffold the repository and Docker Compose environment.
5. Implement M1 as the first complete vertical slice.
6. Update this plan after each milestone based on measured results and newly available data.
