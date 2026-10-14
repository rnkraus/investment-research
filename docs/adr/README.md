# Architecture Decision Record Plan

**Status:** Active — accepted and proposed decisions with a provisional backlog

**Purpose:** Record important architectural decisions when they become implementation constraints

## 1. Working rule

Create an ADR only when a decision:

- is difficult or expensive to reverse;
- affects multiple modules or quality attributes;
- introduces an external dependency or operational commitment;
- resolves a meaningful trade-off with credible alternatives;
- establishes a rule future contributors could otherwise accidentally violate.

Do not create ADRs for routine coding choices, temporary experiments, or details that can be changed locally without architectural impact.

Write the ADR immediately before the decision becomes an implementation constraint, or when an experiment provides enough evidence to decide. Do not write every planned ADR in advance.

## 2. Accepted ADRs

| ADR | Decision | Status |
|---|---|---|
| [ADR-0001](./0001-modular-monolith-and-workers.md) | Modular monolith with separate API, worker, and parser runtime boundaries | Accepted |
| [ADR-0002](./0002-canonical-and-derived-stores.md) | Explicit canonical, rebuildable, transient, and observational store ownership | Accepted |
| [ADR-0003](./0003-workspace-isolation-strategy.md) | Trusted application context, scoped repositories and storage, and workspace-aware relational constraints | Accepted |
| [ADR-0004](./0004-m0-0-parser-isolation-boundary.md) | Narrow synthetic-fixture exception with a mandatory parser-isolation gate in M0 | Accepted |
| [ADR-0005](./0005-at-least-once-processing-delivery.md) | Versioned jobs, canonical execution leases, and bounded at-least-once Celery delivery | Accepted |
| [ADR-0006](./0006-credential-free-parser-runtime.md) | Dedicated parser container on a credential-free isolated network | Accepted |
| [ADR-0007](./0007-local-s3-provider.md) | SeaweedFS for local S3-compatible storage and managed S3 for public deployment | Accepted |
| [ADR-0008](./0008-application-owned-ai-and-vector-adapters.md) | Application-owned OpenAI, Pinecone, and OpenTelemetry adapters for the first dense RAG slice | Accepted |
| [ADR-0009](./0009-stable-source-anchors.md) | Discriminated, immutable PDF-page and pasted-text passage anchors | Accepted |
| [ADR-0010](./0010-transactional-outbox-and-reconciliation.md) | Transactional outbox, bounded repair, and separate scheduler | Accepted for M2 implementation; runtime gates outstanding |
| [ADR-0018](./0018-pinecone-reranking-experiment.md) | Bounded opt-in Pinecone BGE reranking and its public/synthetic inference boundary | Accepted for M1.5 experiment; implementation/adoption pending |
| [ADR-0019](./0019-permanent-local-document-deletion.md) | Permanent scoped local document/answer deletion with durable cleanup intent and preserved cost accounting | Accepted; implementation in progress |
| [ADR-0020](./0020-retained-answer-historical-source-access.md) | Answer-bound historical source access without changing new-answer eligibility | Accepted for M2 implementation; runtime gates outstanding |
| [ADR-0021](./0021-content-free-capture-replay-retention.md) | Minimal scoped replay protection after permanent purge | Accepted for M2 implementation; runtime gates outstanding |

## 3. Proposed ADRs

No ADR is currently proposed. Accepted M2 decisions are linked from
[M2 contracts](../15-m2-contracts.md); acceptance freezes implementation constraints, not a claim
that their runtime changes or tests already exist.

## 4. Planned ADR backlog

| Proposed ID | Decision | Trigger | Expected stage |
|---|---|---|---|
| ADR-0011 | Hybrid retrieval with RRF and multilingual reranking | Direct-RAG retrieval baseline is finalized | M4 |
| ADR-0012 | Typed answers with server-side evidence validation | Answer contract becomes production behavior | M4 |
| ADR-0013 | LangSmith for AI observability with canonical audit in PostgreSQL | Tracing and evaluation integration is finalized | M4 |
| ADR-0014 | OIDC and server-side session boundary | Fixed local identity is replaced before release | M5 |
| ADR-0015 | Public hosting platform | Local release is stable and public deployment begins | M6 |
| ADR-0016 | Representative-corpus retrieval changes | Evaluation justifies BM25, learned sparse, late interaction, or another major retrieval change | M7 |
| ADR-0017 | Bounded LangGraph adoption | A measured multi-step use case passes the agent admission gate | M9 |

IDs are provisional until the corresponding ADR file is created. A backlog entry may be removed, combined, split, or reordered if implementation evidence changes the decision boundary.

ADR-0018 records the M1.5 provider/security approval separately from the broader ADR-0011
retrieval-baseline decision. It does not adopt reranking as the default or close the M4 gate.

## 5. ADR lifecycle

Use these statuses:

- `Proposed` — decision is under review;
- `Accepted` — decision governs current implementation;
- `Deprecated` — retained for history but no longer recommended;
- `Superseded by ADR-XXXX` — replaced by a newer decision.

Accepted ADRs are not rewritten to make history appear cleaner. Minor corrections may be appended, but a changed decision receives a new ADR that supersedes the old one.

## 6. Minimal ADR structure

Each future ADR should contain only:

1. **Title and status**
2. **Context** — the concrete problem and constraints
3. **Decision drivers** — the qualities that matter
4. **Options considered** — realistic alternatives
5. **Decision** — the selected option and scope
6. **Consequences** — benefits, costs, risks, and follow-up work
7. **Validation or reconsideration trigger** — evidence that could change the decision

Target one to two pages. Link to deeper benchmarks, diagrams, or experiments rather than copying them into the ADR.

## 7. Review points

Review the backlog:

- after M0.0 identifies which proof contracts should be retained;
- before M0 repository structure becomes difficult to change;
- before the M1 evidence and storage contracts are finalized;
- before M4 retrieval and answer behavior is treated as a baseline;
- when selecting public hosting in M6;
- after representative-corpus evaluation in M7;
- before any LangGraph workflow is added in M9.

The ADR plan is a reminder, not a requirement to create every listed record.
