# Evidence Infrastructure

This package contains technical adapters used by the Evidence module's application-owned ports.
The adapters provide canonical source state and bounded statement-support decisions; they do not
persist, authorize, or publish generated answer content.

## Contents

| File               | Responsibility                                                             |
| ------------------ | -------------------------------------------------------------------------- |
| `__init__.py`      | Identifies the evidence-adapter package without exporting implementations. |
| `deterministic.py` | Implements the explicit synthetic M0.0 statement-to-evidence support rule. |
| `openai.py`        | Implements the bounded, typed M1 semantic statement-support provider call. |
| `postgresql.py`    | Reloads scoped candidate sources and already-persisted citation sources.   |

## Canonical reload

```mermaid
flowchart LR
    REF[Proposed candidate ID] --> READER[PostgresCanonicalEvidenceSourceReader]
    CTX[Trusted WorkspaceContext] --> READER
    READER --> RC[RetrievalCandidate and Run]
    READER --> CHUNK[Chunk, ChunkSet, and ProcessingRun]
    READER --> BLOCK[StructureBlock]
    READER --> DOC[DocumentVersion and Document]
    RC --> SNAPSHOT[CanonicalEvidenceSource]
    CHUNK --> SNAPSHOT
    BLOCK --> SNAPSHOT
    DOC --> SNAPSHOT
    SNAPSHOT --> VALIDATOR[Evidence application validator]
```

Every join includes the trusted `workspace_id`, and the query also requires the caller-provided
retrieval-run and candidate identifiers to match one stored relationship. Unknown identifiers,
identifiers from another run, and inaccessible workspace data all produce the same `None` result.
The reader returns canonical facts rather than deciding whether they pass current validation rules.

This cross-module physical read belongs in infrastructure because it must join mappings owned by
Search, Processing, and Documents. Business modules continue to avoid importing another module's
`persistence.py`.

## Citation reload

```mermaid
flowchart LR
    ID[Evidence span ID] --> READER[PostgresCanonicalEvidenceSpanReader]
    CTX[Trusted WorkspaceContext] --> READER
    READER --> SPAN[EvidenceSpan]
    SPAN --> VERSION[DocumentVersion]
    VERSION --> DOCUMENT[Document state and current version]
    SPAN --> EXISTS[Validated statement in streamed answer exists]
    DOCUMENT --> SNAPSHOT[CanonicalEvidenceSpanSource]
    EXISTS --> SNAPSHOT
    SNAPSHOT --> SERVICE[Evidence application service]
```

The citation reader filters the main query by the trusted workspace and uses a correlated `EXISTS`
query to establish whether the span participates in a validated statement whose answer reached the
`STREAMED` state. It returns the boolean and current document facts to the application service; it
does not make the authorization decision itself. A short-lived read unit of work owns the SQLAlchemy
session and never commits.

## Statement-support evaluators

The deterministic evaluator accepts only explicitly configured statement and exact-evidence pairs.
It remains useful for the frozen local fixture and deterministic tests.

The M1 OpenAI adapter independently evaluates one complete statement against one or two exact
canonical evidence texts. Its input is limited to 24,000 code points, and its strict response schema
contains only `supported: bool`. The request uses the approved model with low reasoning, a
30-second timeout, provider storage disabled, no tools, no background execution, and SDK retries
disabled. Its prompt, adapter, and configuration versions are separate from answer generation.

```mermaid
flowchart LR
    STATEMENT[Generated complete statement] --> INPUT[Bounded StatementSupportInput]
    EVIDENCE[Exact canonical evidence text] --> INPUT
    INPUT --> ADAPTER[StatementSupportEvaluator adapter]
    ADAPTER --> DECISION[StatementSupportDecision]
    DECISION --> VALIDATOR[EvidenceValidator]
    CANONICAL[Workspace, version, time, retrieval, and exact-span checks] --> VALIDATOR
    VALIDATOR --> OUTPUT[Accept aggregate or fail closed]
```

The evaluator is independent from the answer generator, so a generator cannot approve its own
output. A positive provider decision cannot authorize a citation, document, workspace, range, or
answer. The application-owned `EvidenceValidator` combines the decision with all canonical checks
and releases content only when the complete aggregate passes.

`openai.py` is intentionally not wired into runtime settings or bootstrap in this work block.
Provider selection, usage accounting, lifecycle ownership, and public error mapping are added by
the later M1 generation-policy work.
