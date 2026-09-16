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

## 3. Proposed ADRs

No ADR is currently proposed.

## 4. Planned ADR backlog

| Proposed ID | Decision | Trigger | Expected stage |
|---|---|---|---|
| ADR-0008 | Application-owned RAG core and provider ports | First AI and vector adapters are implemented | M1 |
| ADR-0009 | Stable evidence anchors and versioned processing outputs | Parser, structure-block, chunk, and citation contracts are finalized | M1–M2 |
| ADR-0010 | Transactional outbox and vector-index reconciliation | Reliable reprocessing and deletion are implemented | M2 |
| ADR-0011 | Hybrid retrieval with RRF and multilingual reranking | Direct-RAG retrieval baseline is finalized | M4 |
| ADR-0012 | Typed answers with server-side evidence validation | Answer contract becomes production behavior | M4 |
| ADR-0013 | LangSmith for AI observability with canonical audit in PostgreSQL | Tracing and evaluation integration is finalized | M4 |
| ADR-0014 | Public hosting platform | Local release is stable and public deployment begins | M6 |
| ADR-0015 | Representative-corpus retrieval changes | Evaluation justifies BM25, learned sparse, late interaction, or another major retrieval change | M7 |
| ADR-0016 | Bounded LangGraph adoption | A measured multi-step use case passes the agent admission gate | M9 |

IDs are provisional until the corresponding ADR file is created. A backlog entry may be removed, combined, split, or reordered if implementation evidence changes the decision boundary.

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
