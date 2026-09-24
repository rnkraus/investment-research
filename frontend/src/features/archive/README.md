# Archive Feature

This directory owns the English M1 archive workflow presented by the home page. It coordinates
browser state and calls the shared API client; it does not define transport payloads, access
PostgreSQL, or parse document content.

## Current files

| File                    | Responsibility                                                                       |
| ----------------------- | ------------------------------------------------------------------------------------ |
| `copy.ts`               | Centralizes English feature text and defaults for later localization.                |
| `document-workflow.tsx` | Captures one PDF or pasted-text original and polls its background-processing status. |
| `processing-state.ts`   | Defines polling completion and fail-closed research admission.                       |
| `research-session.ts`   | Reduces validated SSE events into one ordered, fail-closed UI result.                |
| `research-workflow.tsx` | Asks a bounded question and opens resolved PDF-page or text-passage evidence.        |
| `source-anchor.ts`      | Verifies exact pasted-text ranges and checksums using Unicode code points.           |
| `source-viewer.tsx`     | Resolves and renders an authorized immutable PDF or pasted-text source.              |

## Capture and processing flow

```mermaid
flowchart LR
    USER[User chooses source type] --> FORM[DocumentWorkflow]
    FORM -->|PDF multipart| PDF[PDF capture endpoint]
    FORM -->|Text JSON| TEXT[Pasted-text capture endpoint]
    PDF --> ACCEPTED[DocumentAccepted]
    TEXT --> ACCEPTED
    ACCEPTED --> QUEUE[Processing task dispatch]
    QUEUE --> WORKER[Celery worker]
    WORKER --> RUNNER[Isolated parser runner]
    FORM --> POLL[Poll processing status]
    RUNNER --> STATUS[Canonical ProcessingRun state]
    STATUS --> POLL
    POLL --> LEXICAL[LEXICAL_READY]
    LEXICAL --> INDEXING[INDEXING]
    INDEXING --> SEARCHABLE[SEARCHABLE]
    SEARCHABLE --> READY[Ready for research]
```

Both capture responses supply the same processing status URL and canonical identifiers. The browser
never starts parsing inside the API process. Each capture endpoint commits the canonical `CAPTURED`
run and publishes its task to Celery, and the component polls the workspace-scoped status endpoint
until the run reaches `SEARCHABLE` or `FAILED`. Both `LEXICAL_READY` and `INDEXING` remain active
M1 states and therefore continue polling without exposing the research form. The worker invokes the
isolated parser runner through protocol v2 while the historical protocol-v1 PDF path remains a
backend regression contract.

All network access goes through `src/lib/api/client.ts`. Raw exceptions are not rendered. The
component displays only safe API messages or a generic local failure message, and active requests
are aborted when their component lifecycle ends.

## Research state flow

```mermaid
flowchart LR
    START[RUNNING session] --> EVENT[Validated SSE event]
    EVENT --> STATEMENT[Add unique statement]
    STATEMENT --> CITATION[Attach ordered citation reference]
    CITATION --> COMPLETE{Terminal event}
    COMPLETE -->|answer.completed and every statement cited| RESULT[COMPLETED]
    COMPLETE -->|answer.insufficient and no statements| EMPTY[INSUFFICIENT]
    COMPLETE -->|research.failed and no statements| FAILED[FAILED]
    EVENT -->|wrong request, order, or relationship| REJECT[ResearchSequenceError]
```

`research-session.ts` has no React or network dependency. It keeps streamed events tied to one
research request, rejects duplicate or out-of-order relationships, and refuses to complete an answer
whose statement has no citation. Resolving a citation remains a separate authorized API call; only
then does `attachCitationSource` add the exact source location to the completed UI state.

The UI groups six actual progress events into four stable, English display steps:

| Display step                                  | Backend stages                            |
| --------------------------------------------- | ----------------------------------------- |
| Preparing the question for search             | `EMBEDDING_QUESTION`                      |
| Finding relevant passages                     | `SEARCHING_SOURCES`, `SELECTING_PASSAGES` |
| Drafting an answer from the selected passages | `GENERATING_ANSWER`                       |
| Checking the answer and its evidence          | `VALIDATING_EVIDENCE`, `SAVING_RESULT`    |

A step is active or complete only after its corresponding event arrives; no percentage or timed
animation implies unobserved progress. The credential-free lexical path omits the embedding step.
The answer and citations remain hidden until the terminal validated events arrive.

## Research UI flow

```mermaid
flowchart LR
    READY[SEARCHABLE document] --> FORM[ResearchWorkflow]
    FORM --> STREAM[Typed research event stream]
    STREAM --> SESSION[Research session reducer]
    SESSION -->|validated completion| IDS[Evidence span IDs]
    IDS --> RESOLVE[Authorized evidence endpoint]
    RESOLVE --> SAFE[Verified stable source path]
    SAFE --> SOURCE[SourceViewer route]
    SOURCE --> ORIGINAL[Authorized immutable original]
    ORIGINAL --> LOCATOR{Locator kind}
    LOCATOR -->|PDF_PAGE| PAGE[PDF blob at stored physical page]
    LOCATOR -->|TEXT_PASSAGE| TEXT[Verified exact-range highlight]
    SESSION -->|insufficient evidence| EMPTY[Explicit empty result]
    SESSION -->|invalid sequence| REJECT[Discard partial result]
```

`document-workflow.tsx` mounts the research UI only for the M1 `SEARCHABLE` state. The UI submits the
current document ID as an explicit research filter and never treats streamed partial text as a
usable answer. After a validated terminal event, each unique evidence span is resolved through the
workspace-scoped API. The resulting viewer URL must still pass the frontend's stable local
source-path check
before it becomes an original-source link. The discriminated locator renders either the PDF page or
the one-based visible text range without inventing pages for text. This avoids trusting an arbitrary
URL returned over the network and keeps exact source text separate from the streamed answer
contract.

The stable `/sources/{evidence_span_id}` page resolves the evidence span again and requires the API
to confirm that exact opaque route. It then loads the original through the authorized same-origin
endpoint. PDF bytes are exposed only through a short-lived browser object URL with the stored
physical page fragment. Pasted text is split with `Array.from`, so supplementary-plane characters
count as one Unicode code point just as they do in Python. The stored range must reproduce
`exact_text` and its SHA-256 checksum before the viewer renders the original with only that range
inside a `<mark>` element. Unknown media, mismatched routes, invalid ranges, and changed text fail
closed without rendering source content.
