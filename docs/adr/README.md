# Architecture Decision Record Plan

**Status:** Planning only — no individual ADRs created yet  
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

## 2. Planned ADR backlog

| Proposed ID | Decision | Trigger | Expected stage |
|---|---|---|---|
| ADR-0001 | Modular monolith with asynchronous workers | Repository and application boundaries are finalized | M0 |
| ADR-0002 | Canonical versus derived data ownership | Persistent stores and recovery behavior are implemented | M0–M1 |
| ADR-0003 | Workspace-isolation strategy | First schema and authorization repositories are implemented | M1 |
| ADR-0004 | Application-owned RAG core and provider ports | First AI, vector, and object-store adapters are implemented | M1 |
| ADR-0005 | Stable evidence anchors and versioned processing outputs | Parser, structure-block, chunk, and citation contracts are finalized | M1–M2 |
| ADR-0006 | Transactional outbox and vector-index reconciliation | Reliable reprocessing and deletion are implemented | M2 |
| ADR-0007 | Hybrid retrieval with RRF and multilingual reranking | Direct-RAG retrieval baseline is finalized | M4 |
| ADR-0008 | Typed answers with server-side evidence validation | Answer contract becomes production behavior | M4 |
| ADR-0009 | LangSmith for AI observability with canonical audit in PostgreSQL | Tracing and evaluation integration is finalized | M4 |
| ADR-0010 | Public hosting platform | Local release is stable and public deployment begins | M6 |
| ADR-0011 | Representative-corpus retrieval changes | Evaluation justifies BM25, learned sparse, late interaction, or another major retrieval change | M7 |
| ADR-0012 | Bounded LangGraph adoption | A measured multi-step use case passes the agent admission gate | M9 |

IDs are provisional until the corresponding ADR file is created. A backlog entry may be removed, combined, split, or reordered if implementation evidence changes the decision boundary.

## 3. ADR lifecycle

Use these statuses:

- `Proposed` — decision is under review;
- `Accepted` — decision governs current implementation;
- `Deprecated` — retained for history but no longer recommended;
- `Superseded by ADR-XXXX` — replaced by a newer decision.

Accepted ADRs are not rewritten to make history appear cleaner. Minor corrections may be appended, but a changed decision receives a new ADR that supersedes the old one.

## 4. Minimal ADR structure

Each future ADR should contain only:

1. **Title and status**
2. **Context** — the concrete problem and constraints
3. **Decision drivers** — the qualities that matter
4. **Options considered** — realistic alternatives
5. **Decision** — the selected option and scope
6. **Consequences** — benefits, costs, risks, and follow-up work
7. **Validation or reconsideration trigger** — evidence that could change the decision

Target one to two pages. Link to deeper benchmarks, diagrams, or experiments rather than copying them into the ADR.

## 5. Review points

Review the backlog:

- before M0 repository structure becomes difficult to change;
- before the M1 evidence and storage contracts are finalized;
- before M4 retrieval and answer behavior is treated as a baseline;
- when selecting public hosting in M6;
- after representative-corpus evaluation in M7;
- before any LangGraph workflow is added in M9.

The ADR plan is a reminder, not a requirement to create every listed record.
