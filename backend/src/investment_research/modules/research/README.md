# Research

This module owns immutable research requests, versioned answer lifecycles, and validated answer statements. It orchestrates Search and exposes the application-owned generation boundary; it does not own retrieval candidates, source spans, or validation records.

## Contents

| File             | Responsibility                                                                         |
| ---------------- | -------------------------------------------------------------------------------------- |
| `application.py` | Orchestrates atomic retrieval and validated terminal-answer persistence.               |
| `contracts.py`   | Defines strict retrieval, generation, and validated-answer boundary values.            |
| `domain.py`      | Defines requests, temporal modes, answer states, coverage, statements, and invariants. |
| `ports.py`       | Defines repositories, units of work, and the answer-generator contract.                |
| `persistence.py` | Maps three PostgreSQL tables and implements workspace-scoped repository behavior.      |

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
    INPUT[ResearchRetrievalInput] --> USECASE[AnswerResearchRequestService]
    USECASE --> RETRIEVE[RetrieveResearchService]
    RETRIEVE --> REQUEST[ResearchRequest]
    INPUT --> FILTERS[Effective RetrievalFilters]
    FILTERS --> QUERY[LexicalSearchQuery]
    REQUEST --> UOW[ResearchRetrievalUnitOfWork]
    QUERY --> UOW
    UOW --> SEARCH[LexicalSearch]
    SEARCH --> HITS[Ordered eligible hits]
    HITS --> CANDIDATES[Ranked RetrievalCandidates]
    CANDIDATES --> RUN[RetrievalRun plus candidates]
    UOW -->|one commit or rollback| PG[(PostgreSQL)]
    RUN --> RESULT[ResearchRetrievalResult]
    RESULT --> GENERATE[GenerateValidatedAnswerService]
    GENERATE --> OUTPUT[ValidatedAnswerResult]
```

The service creates all identifiers server-side, retains the exact question, normalizes only its
search form, and maps each ordered hit to one persisted `LEXICAL` candidate. An empty hit sequence is
a valid reproducible retrieval result and does not create invented candidates. The returned
`ResearchRetrievalResult` keeps each persisted candidate aligned with the canonical hit data needed
by the later generation and evidence path.

`AnswerResearchRequestService` is the route-facing application use case. It passes the committed
retrieval result directly to `GenerateValidatedAnswerService` and returns only the validated
terminal result. This small composition keeps HTTP code unaware of intermediate retrieval data and
preserves the two focused services as independently testable boundaries.

## Answer-generation boundary

```mermaid
flowchart LR
    RESULT[ResearchRetrievalResult] --> INPUT[AnswerGenerationInput]
    INPUT --> PORT[AnswerGenerator]
    PORT --> RAW[UnvalidatedGeneratedAnswer]
    RAW --> UOW[AnswerValidationUnitOfWork]
    UOW --> VALIDATION[Canonical evidence validation]
    VALIDATION -->|accepted answer| STREAMED[STREAMED plus statements and evidence]
    VALIDATION -->|insufficient archive| INSUFFICIENT[INSUFFICIENT without statements]
    VALIDATION -->|failed rule| REJECTED[REJECTED without generated content]
    STREAMED --> PG[(One PostgreSQL transaction)]
    INSUFFICIENT --> PG
    REJECTED --> PG
```

`AnswerGenerationInput` contains the exact question and a bounded, ordered snapshot of the selected
retrieval sources. Each source keeps its retrieval-candidate, chunk, document-version, structure,
page, and text identity. A generated evidence reference points to a candidate and an exact text
range; it is only a proposal until the server reloads and validates canonical data.

The generator result is immutable and schema-valid but intentionally named `Unvalidated`. It may
never be persisted as an accepted statement or emitted to a client merely because its Pydantic
schema parsed successfully. The `AnswerGenerator` port lets M0.0 use a deterministic fixture adapter
and later use an approved model adapter without changing this trust boundary.

`GenerateValidatedAnswerService` calls the generator before opening a database transaction. It then
reloads every proposed source through the canonical Evidence reader and maps only the validator's
safe decision to domain records. Visible streamed text is rebuilt from accepted statements rather
than copied from the generator's untrusted `rendered_text`. Insufficient output uses an
application-owned message, and rejected output retains safe rule findings but no generated content.

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

Every table and relationship carries `workspace_id`. Unknown and inaccessible identifiers produce
the same repository result. Repositories stage changes but never commit; the application service
owns the unit of work across Research and Evidence. For a streamed answer it flushes the staged
answer, statements, and spans before adding their foreign-key dependents. This is an ordering
boundary inside the same transaction, not an early commit, so any later failure still rolls back the
complete aggregate.

Step 6 creates and atomically persists the request, lexical retrieval run, and selected candidates.
Step 7 now provides the typed generator boundary, canonical evidence validation, and atomic terminal
answer persistence. `AnswerResearchRequestService` composes those operations as one complete use
case. The HTTP route consumes only `ValidatedAnswerResult` and maps it to versioned terminal stream
events; it never receives an `UnvalidatedGeneratedAnswer`. Resolving a citation identifier to an
authorized source location remains a separate Evidence application and HTTP boundary.
