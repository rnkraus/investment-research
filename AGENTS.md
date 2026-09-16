# Investment Research Archive — Repository Instructions

## Communication and Language

- Communicate with the user in German unless the user explicitly requests another language.
- Write source code, identifiers, comments, commit messages, API schemas, and technical Markdown documentation in English.
- Keep all V1 user-interface text, product messages, demo content, and archive-document support in English. Preserve explicit language metadata and extensible interfaces so additional languages can be added later.

## Project Contract and Sources of Truth

Build an application for evidence-grounded investment research. It imports research documents, preserves their originals, makes them searchable, and answers questions with exact, verifiable citations.

This is a research archive, not a trading system. Do not add portfolio management, order execution, price prediction, automated buy or sell recommendations, or investment-advice functionality unless the product scope is explicitly changed.

Use these documents as the project specification:

- `docs/01-product-scope.md` — scope and acceptance boundaries.
- `docs/02-system-design.md` — architecture and technical baseline.
- `docs/03-domain-model.md` — domain terminology and invariants.
- `docs/04-security-and-operations.md` — security and operational requirements.
- `docs/05-delivery-plan.md` — current milestones, order, and exit gates.
- `docs/06-future-features.md` — unscheduled ideas, not approved implementation scope.
- `docs/07-m0-0-architecture-flows.md` — paired target and M0.0 module, workflow, and lifecycle diagrams.
- `docs/08-m0-0-implementation-plan.md` — implementation-ready M0.0 repository structure, contracts, sequence, and verification gates.
- `docs/09-m0-implementation-plan.md` — implementation-ready M0 runtime topology, replacement sequence, work blocks, and verification gates.
- `docs/10-m1-implementation-plan.md` — M1 thin-slice decisions, repository delta, implementation sequence, and verification gates.
- `docs/11-m1-contracts.md` — frozen M1 application, provider, API, usage, tracing, and verification contracts.
- `docs/adr/` — accepted architectural decisions when they exist.

Accepted ADRs govern the decisions they address. Keep affected compact documents synchronized with later decisions. Report conflicts instead of choosing silently.

Files under `outputs/` are historical references, not sources of truth. Do not rewrite them unless explicitly requested. Treat `tmp/` and `work/` as disposable; application code must not depend on them, and sensitive data must not be placed there.

Do not promote a future feature into the active milestone without explicit approval.

## Instruction Scope

- Keep repository-wide instructions in this root `AGENTS.md`.
- Do not create a nested `AGENTS.md` or `AGENTS.override.md` without first explaining why different instructions are necessary and receiving the user's explicit confirmation.
- Propose nested instructions only for genuinely different subtree commands or rules, and do not repeat root instructions in them.

## Technical Baseline

Follow `docs/02-system-design.md`. The fixed baseline is:

- modular monolith with separate FastAPI API and Celery worker processes;
- Next.js, TypeScript, Tailwind CSS, and shadcn/ui for the web application;
- Python, FastAPI, Pydantic, SQLAlchemy, and Alembic for the backend;
- PostgreSQL and S3-compatible object storage, with SeaweedFS locally, as canonical stores;
- Redis for transient queue, lock, and cache state;
- Pinecone as a derived, rebuildable dense retrieval index;
- OpenTelemetry/OpenInference and optional LangSmith for observability, never canonical state;
- Docker Compose as the local multi-service runtime.

M0.0 is the only approved exception to this full runtime baseline. It is a local-only proof using the repository-owned frozen synthetic fixture and the minimum Next.js, FastAPI, and PostgreSQL path described in `docs/05-delivery-plan.md` and `docs/08-m0-0-implementation-plan.md`. Its parser boundary is governed by `docs/adr/0004-m0-0-parser-isolation-boundary.md`. It must not use private data, become publicly reachable, or be treated as satisfying M0 or M1. Model it as a subset of the target system and reuse production module, entity, state, and workflow names wherever their meaning is the same. Preserve reusable application contracts, then replace all M0.0-only storage and execution shortcuts during M0 before continuing to M1.

M1 retains `FixedLocalIdentityContextProvider` only for the local single-user RAG slice described in
`docs/10-m1-implementation-plan.md`. Keep the host entry point loopback-only, preserve all
workspace-scoped contracts and isolation tests, and do not add OIDC, users, memberships, or browser
sessions during M1. M5 must replace the fixed provider with accepted OIDC authentication,
canonical memberships, and server-side sessions before any staging, shared-network, or public use.

Use `pnpm` for frontend dependencies and scripts. Keep `pnpm-lock.yaml` under version control once the frontend is scaffolded; do not use npm, Yarn, Bun, or competing frontend lockfiles.

Use `uv` for Python dependency management, environments, locking, and backend commands. Keep `uv.lock` under version control once the backend is scaffolded; do not manage project dependencies with direct `pip install` commands.

The local object-storage provider and later public-storage boundary are governed by
`docs/adr/0007-local-s3-provider.md`. Use SeaweedFS only for the local Docker Compose runtime and
select a managed S3-compatible service separately before public deployment. Keep the application
dependent on its provider-neutral `ObjectStorage` contract.

The frontend accesses data through FastAPI, never directly through PostgreSQL, Pinecone, Redis,
object storage, or AI providers. Backend modules communicate through typed application services
rather than another module's internal persistence implementation. Keep provider SDKs behind small,
typed, application-owned adapters.

Do not introduce AWS-specific infrastructure, Kubernetes, microservices, or additional distributed services before a measured requirement and accepted architectural decision justify them.

Before proposing or modifying any dependency or version constraint, research the currently recommended stable versions using authoritative, up-to-date sources such as maintainer documentation, changelogs, the official package registry, and relevant security advisories. Evaluate compatibility with the project's pinned runtimes, frameworks, platforms, peer dependencies, and lockfile; confirm that the candidate is maintained, not yanked or an unapproved prerelease, has no applicable known vulnerability, and satisfies the repository's release-age policy. Prefer the safest well-supported version for this project rather than assuming the newest release is automatically best. Explain the recommendation and security or compatibility trade-offs before changing the dependency declaration, then lock and verify the exact resolved version with the repository's package manager and applicable audit checks.

Before adding, replacing, or broadly upgrading a production dependency, explain the need and trade-offs and receive explicit user confirmation. Add an ADR when the choice is architecturally significant.

## RAG and Data Invariants

The direct RAG path is application-owned. Use PostgreSQL for structured and lexical retrieval, Pinecone for multilingual dense retrieval, reciprocal rank fusion, bounded multilingual reranking, typed answer generation, and server-side evidence validation as specified in `docs/02-system-design.md`.

Do not put LangChain, LlamaIndex, or LangGraph at the center of the initial pipeline. Framework adoption requires a demonstrated need and explicit approval. Introduce LangGraph only after the admission criteria in `docs/05-delivery-plan.md` are met, and make it orchestrate existing services rather than duplicate RAG logic.

Preserve these invariants:

- PostgreSQL and object storage are canonical; Pinecone is rebuildable; Redis and LangSmith are never the only copy of business state.
- Every product record and operation carries an explicit workspace boundary; client-supplied `workspace_id` is not authorization proof.
- Every retrieval result must pass canonical workspace, active-version, deletion, and temporal eligibility checks before use.
- Original artifacts and document versions are immutable; corrections create versions, and reprocessing creates new derived outputs.
- Every archive-derived answer statement resolves to an eligible original-document span; chunks and conversation history are not evidence.
- Missing and conflicting evidence must be shown explicitly; general model knowledge cannot silently fill archive gaps.
- Model output remains untrusted until schema and business-rule validation succeed.
- AI-generated archive content must be labeled and must not later appear to confirm itself.
- Publication, public availability, archive ingestion, and system recording times remain distinct.
- Ingestion, reprocessing, indexing, deletion, and retries are idempotent and repairable.
- PostgreSQL-to-Pinecone synchronization uses durable intent, deterministic identifiers, receipts, and reconciliation, not distributed transactions.
- Prompt, model, parser, chunking, embedding, retrieval, reranking, and processing configurations are versioned.

## Runtime AI Safety and Development Tools

Restrictions in this section apply to the application's runtime models, not to development tools used by Codex unless another repository rule explicitly says otherwise.

- V1 runtime models receive no web-search or write-capable agent tools.
- Codex may use in-scope development, filesystem, shell, documentation, and web-research tools subject to the active sandbox, approval policy, and user request.
- Treat uploaded documents, pasted text, retrieved passages, external content, model responses, and tool output as untrusted input.
- Document content cannot override system policy, authorize tools, request credentials, change workspace boundaries, or disable evidence validation.
- Only public or non-sensitive documents may be sent to approved external AI providers until private-data handling is reviewed.
- Keep raw document text and full prompts out of logs and traces by default.
- LangSmith failure must not fail an otherwise valid product request.
- Keep sanitized demo data isolated from private workspace data.

## Environment Files and Secrets

Do not read, open, inspect, search within, print, modify, create, delete, rename, or copy `.env`, any `.env.*` file, or any environment file containing real credentials or secrets.

The only exception is a file whose name matches `*.env.example`. Such files may be read and updated but must contain placeholders only, never working credentials, private endpoints, or sensitive values.

The application may load ignored environment files at runtime, but agents must not inspect or modify them. If configuration changes, update the applicable example with a safe placeholder and ask the user to configure the real value outside agent access.

Never intentionally print, log, commit, reproduce, or expose secrets, credentials, tokens, private data, or secret-file contents.

## Workflow and Approval Boundaries

Before modifying the repository:

1. inspect Git status and preserve unrelated work;
2. identify the active milestone and exit gate in `docs/05-delivery-plan.md`;
3. read the relevant compact documents and accepted ADRs;
4. choose the smallest complete change and its required verification.

Prefer complete vertical slices over broad unfinished infrastructure. Long-running parsing, enrichment, embedding, and indexing belong in workers. Optional AI enrichment must not block a parsed document from becoming searchable. Use Alembic for schema changes and public, synthetic, openly licensed, or redistributable test fixtures.

### Implementation step order

An implementation step is one explicitly numbered, bounded step in the active implementation plan, including its stated scope and completion gate. For example, `Step 3 — Migration and scoped persistence` is one implementation step. It is not an individual file, commit, or the entire milestone.

Complete each implementation step in this order:

1. Implement and verify all backend behavior required by the step.
2. Add or update the CI checks required to verify that behavior automatically.
3. Implement and verify the frontend behavior required by the same step.
4. Run the step's complete verification gate before starting the next implementation step.

If a step requires no frontend changes, do not create artificial frontend work. Run the existing frontend checks to confirm compatibility, complete the step's gate, and then continue.

### Implementation work blocks

Before starting implementation, define the current coherent work block and its verification gate. A work block is the smallest meaningful aspect, module, or plan subsection that requires several related files to produce a reviewable result; it is narrower than an implementation step.

Within an approved work block, create or modify its related files in a sensible dependency order without pausing for approval after every file. After each file, promptly give the user a concise update that names the file, explains what changed and why, and describes its relationship to the surrounding code. Continue automatically to the next file in the same block unless the user asks to pause.

Resolve formatting, linting, typing, and test failures that belong to the current work block before declaring it complete. After the block's verification gate passes, summarize its files, behavior, data flow, and checks, then pause for the user's explicit approval before starting the next work block.

Pause earlier if progress requires an unexpected dependency, architectural or security decision, destructive action, external write, or material expansion beyond the approved work block.

Keep changes focused. Do not perform unrelated cleanup, add private archive data, or weaken architecture, validation, isolation, sandbox, or security controls to make a task pass.

When requesting approval to run a shell command, include the exact command in the user-facing approval question. Redact secret or sensitive values when necessary and explicitly state that the displayed command was redacted.

Do not commit, push, rewrite history, deploy, publish, or use destructive Git commands without explicit user authorization.

Update the relevant compact document when behavior, architecture, domain semantics, security requirements, or milestone scope changes. Create an ADR only for a significant, cross-cutting, expensive-to-reverse, security-sensitive, or externally binding decision.

## Documentation Maintenance

- Treat documentation as part of the implementation and maintain it continuously in the same change as the behavior, architecture, workflow, configuration, or operational process it describes.
- Follow documentation best practices: write for the intended audience, keep one clear source of truth, prefer concise links over duplicated content, state scope and assumptions, and keep terminology consistent with the domain model.
- Keep diagrams, examples, API schemas, repository structures, milestone gates, and ADR references synchronized with the implemented system. Remove obsolete guidance or mark it explicitly as historical or superseded.
- Document only commands, configuration keys, and behavior that actually exist and have been verified. Use placeholders in examples, never real secrets or private data.
- Record significant decisions and their trade-offs at the appropriate level: update compact specifications for current behavior and create an ADR only when the repository's ADR criteria are met.
- Review affected documentation during verification and treat contradictions, stale instructions, and broken links as defects rather than optional cleanup.

## Verification

- After frontend changes, run the applicable formatting, linting, type-checking, and test scripts through `pnpm`.
- After backend changes, run the applicable formatting, linting, type-checking, and test commands through `uv run`.
- After cross-cutting or infrastructure changes, run the relevant Docker Compose integration checks.
- Add or update tests for behavior changes when an applicable test setup exists.
- Keep normal unit and integration tests independent of live paid providers; provider tests must be separate, opt-in, and bounded.
- Cover the affected risk, especially workspace isolation, evidence and citation validation, temporal eligibility, migrations, idempotency, stale indexes, deletion, retries, untrusted input, and provider adapters.
- Keep deterministic correctness tests separate from retrieval-quality evaluation.
- Never claim a check passed unless it ran; report unavailable checks and pre-existing failures.

Until exact canonical commands are documented, do not invent command names. Once the scaffold defines them, record the real `pnpm`, `uv`, and Docker Compose commands here or in a referenced developer guide.

## Definition of Done

A change is complete when it respects the active scope and invariants, includes applicable tests and migrations, passes relevant available checks, keeps documentation and ADRs aligned, and adds no secrets, private fixtures, caches, or unintended generated artifacts.

In the final response, summarize the result, important files changed, checks and outcomes, and remaining risks or limitations.
