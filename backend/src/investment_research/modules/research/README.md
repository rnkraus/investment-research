# Research

This module owns immutable research requests, versioned answer lifecycles, and validated answer statements. It orchestrates Search and Evidence through typed contracts in later implementation steps; it does not own retrieval candidates, source spans, or validation records.

## Contents

| File             | Responsibility                                                                       |
| ---------------- | ------------------------------------------------------------------------------------ |
| `application.py` | Orchestrates one atomic request and bounded lexical retrieval.                       |
| `contracts.py`   | Defines strict retrieval input and the aligned request, run, candidate, and hit result. |
| `domain.py`      | Defines requests, temporal modes, answer states, coverage, statements, and invariants. |
| `ports.py`       | Defines repository and cross-module research-retrieval unit-of-work contracts.       |
| `persistence.py` | Maps three PostgreSQL tables and implements workspace-scoped repository behavior.     |

## Ownership and traceability

```mermaid
flowchart LR
    RQ[ResearchRequest<br/>exact question and context] --> RR[RetrievalRun<br/>owned by Search]
    RQ --> A[Answer<br/>versioned lifecycle]
    RR --> A
    A --> AS[AnswerStatement<br/>validated archive claim]
    AS --> ES[Evidence links<br/>added by Evidence]
```

An answer references the exact retrieval run that supplied its context. The composite database relationship includes workspace, request, and run identifiers, so a run from another request or workspace cannot be attached accidentally.

Research imports only typed domain contracts from dependencies when application orchestration requires them. It never imports another module's `persistence.py`.

## Temporal request context

```text
CURRENT               → no time cutoff
PUBLIC_KNOWLEDGE      → available_at <= cutoff
PERSONAL_ARCHIVE      → available_at <= cutoff and captured_at <= cutoff
```

The request preserves the user's exact question and explicit document filters. The Search module
separately records the normalized query and effective retrieval-filter snapshot because those are
execution details needed to reproduce a specific run. `RetrieveResearchService` performs the mode
translation once, so Search receives concrete predicates and does not depend on Research enums.

## Lexical retrieval orchestration

```mermaid
flowchart LR
    INPUT[ResearchRetrievalInput] --> REQUEST[ResearchRequest]
    INPUT --> FILTERS[Effective RetrievalFilters]
    FILTERS --> QUERY[LexicalSearchQuery]
    REQUEST --> UOW[ResearchRetrievalUnitOfWork]
    QUERY --> UOW
    UOW --> SEARCH[LexicalSearch]
    SEARCH --> HITS[Ordered eligible hits]
    HITS --> CANDIDATES[Ranked RetrievalCandidates]
    CANDIDATES --> RUN[RetrievalRun plus candidates]
    UOW -->|one commit or rollback| PG[(PostgreSQL)]
```

The service creates all identifiers server-side, retains the exact question, normalizes only its
search form, and maps each ordered hit to one persisted `LEXICAL` candidate. An empty hit sequence is
a valid reproducible retrieval result and does not create invented candidates. The returned
`ResearchRetrievalResult` keeps each persisted candidate aligned with the canonical hit data needed
by the later generation and evidence path.

## Answer lifecycle

```mermaid
stateDiagram-v2
    [*] --> REQUESTED
    REQUESTED --> RETRIEVING
    RETRIEVING --> GENERATING
    RETRIEVING --> INSUFFICIENT
    GENERATING --> VALIDATING
    GENERATING --> REJECTED
    VALIDATING --> STREAMED
    VALIDATING --> INSUFFICIENT
    VALIDATING --> REJECTED
    REJECTED --> REGENERATED
```

`REGENERATED` closes the rejected attempt; regeneration creates a new answer row with the next version number. Only `STREAMED` and `INSUFFICIENT` contain user-visible rendered text. A streamed answer has `COMPLETE`, `PARTIAL`, or `CONFLICTING` archive coverage. An insufficient answer explicitly uses `INSUFFICIENT` coverage and has no archive-derived statements.

Answer statements represent accepted archive-derived claims, not raw model output. M0.0 persists only the `ARCHIVE_DERIVED` statement type with `VALIDATED` support status. The Evidence module adds the required source links and validation records in the same caller-owned transaction before any statement is emitted.

## Transaction and isolation boundary

```mermaid
flowchart LR
    CTX[Trusted WorkspaceContext] --> REPO[ResearchRepository]
    DATA[Request, answer, or statements] --> REPO
    REPO --> CHECK[Scope, lifecycle,<br/>identity, and order checks]
    CHECK --> SESSION[Caller-owned SQLAlchemy Session]
    SESSION -->|commit all or rollback all| PG[(PostgreSQL)]
```

Every table and relationship carries `workspace_id`. Unknown and inaccessible identifiers produce the same repository result. The repository stages changes but never commits; the application service owns the unit of work across Research, Search, and Evidence.

Step 6 now creates and atomically persists the request, lexical retrieval run, and selected
candidates. Answer generation, evidence validation, and safe output begin in Step 7.
