# M2 lifecycle contracts — version 1

**Status:** Accepted for M2 implementation on 2026-10-14; Step 0 documentation contract frozen.
No endpoint, schema, numeric policy, migration, or process below is implemented by this document.

The owner approved the product policies, then the initial limits and content-free replay protection.
ADRs [0010](./adr/0010-transactional-outbox-and-reconciliation.md),
[0020](./adr/0020-retained-answer-historical-source-access.md), and
[0021](./adr/0021-content-free-capture-replay-retention.md) record the cross-cutting decisions.
Implementation must pass its owning step's tests before runtime activation; a failed gate requires
a visible correction or contract amendment, never silently relaxed bounds.

**Scope:** Extend the accepted local M1/M1.5 runtime without changing RAG policy, provider budgets,
fixed identity, or loopback-only operation. The [M2 plan](./12-m2-implementation-plan.md) owns
sequence and gates. [M1 contracts](./11-m1-contracts.md) remain the implemented compatibility baseline.

## 1. Authorities and ownership

| Owner | Durable authority | Must not become a second authority |
| --- | --- | --- |
| Documents | Immutable versions/originals, current-version pointer, capture replay and duplicate provenance, deletion receipts | Catalog snapshots and client selection |
| Processing | Run readiness, stage output, attempt history, execution fences, promotion readiness | Celery result state or broker delivery count |
| Delivery | Outbox claims, publication attempts, due times, repair history | Processing success or source eligibility |
| Search | Desired vector identities, write/visibility receipts, reconciliation results | Pinecone metadata as authorization |
| Evidence/research | Retained validated statement/span relationships and exact original anchors | Chunks, context displays, or previous model prose as evidence |

Step 1 uses the existing `ProcessingRun` as its business job. A later `Job` view is a projection of
that work, not an independently writable success state. Cross-module atomic writes use declared
unit-of-work ports and one infrastructure-owned SQLAlchemy transaction; repositories do not commit.

## 2. Capture, replay, and versions

The two existing single-document capture endpoints retain `202 DocumentAccepted` and their current
required fields. Step 1 changes only durable delivery. Step 3 adds an optional `Idempotency-Key`
UUID header to those endpoints; existing clients without it retain intentional-import semantics.
New version and batch items require a client-created UUID request key. Workspace always comes
from trusted server context, never the submitted key or body.

The replay scope is `(workspace_id, operation, target_document_id-or-null, request_key)`. Its
fingerprint covers canonical original bytes, media type, validated title/language/source dates,
and expected active-version state. Text bytes use ADR-0009 newline normalization. HTTP multipart
boundaries and transport filenames are not content identity. Same key and fingerprint returns
the original capture identifiers and `202`, even if processing has since completed. It does not
publish a second logical job. Same key with different input returns `409 CONFLICT`.

Concurrent same-key captures must converge through a database uniqueness constraint, not a
check-then-insert race. Store only committed capture outcomes as replay results. A lost response
requires retrying the same key; never assume a connection error proves rollback. Exact-object
compensation requires proof that no committed capture owns the object; uncertain commit outcomes
must not delete a possibly canonical original.

Keep replay records while the captured document exists. On permanent deletion, retain only a
content-free used-key tombstone with scope and deletion time: no input fingerprint, title, text,
object key, or deleted capture response. Reusing it returns `409 CONFLICT`, never recreates deleted
content, and does not act as authorization. The owner-approved retention extension is defined in
ADR-0021 alongside unchanged ADR-0019 source/answer removal and cost retention. Retain this minimal
protection for as long as the workspace's deletion receipts; never expire it in a periodic retry
loop. A separately reviewed workspace-retirement policy is outside local M2 scope.

A separate request key for identical bytes is an intentional import, not replay. Detect exact
duplicates within one workspace and media type using the canonical SHA-256; preserve independent
originals and provenance. Record one deterministic related retained version, ordered by capture
time and version ID, rather than an unbounded all-pairs graph. Remove source-bound duplicate
relations during purge. Identical text across workspaces never causes shared objects or disclosure.

### Additive version endpoints for M2

| Method and path | Input | Result |
| --- | --- | --- |
| `POST /api/v1/documents/{document_id}/versions` | PDF multipart metadata/file plus `expected_current_version_id`; required key header | `202 DocumentAccepted` for the pending version |
| `POST /api/v1/documents/{document_id}/versions/text` | Existing text metadata/text plus `expected_current_version_id`; required key header | Same accepted shape |
| `GET /api/v1/documents/{document_id}/versions` | `page_size` plus optional paired `after_version_number`/`after_version_id` cursor | `items` and `next_cursor`; version ID/number, immutable metadata, `is_current`, latest processing status; no storage targets |
| `POST /api/v1/documents/{document_id}/reprocessing-runs` | `document_version_id`, `expected_current_version_id`, `expected_active_processing_run_id`; required key header | `202 DocumentAccepted` identifying a new run for the existing immutable version |

Reprocessing is initially limited to the current version, using the server's current allowed
parser/chunk/embedding/index configuration snapshot, not arbitrary client-supplied models or
provider destinations. Matching-key replay returns the original run even if server configuration
later changes. Capture-version replay and reprocessing replay use separate operation scopes.
Version-list metadata does not authorize downloading arbitrary inactive-version originals.
Use the inherited 20-item default and 100-item ceiling for version listing, ordered by version
number then version ID. Cursor fields are both present or both absent; neither grants access.

Lock the scoped active document before allocating the next unique version number. Initially allow
one unfinished replacement/reprocessing candidate per document. A mismatched expected pointer or
competing candidate returns `409`; unknown, foreign, or deletion-pending targets return the same
`404`. Replays are resolved before expected-pointer conflict checks, but never bypass deletion.
A version operation is not an update to an immutable original.

For replacement or reprocessing, retain shadow blocks/chunks and deterministic vectors. Switch
the current-version pointer, active chunk-set/run, and successful aggregate readiness together in
one expected-state transaction after required stage and visibility checks. A failed candidate
leaves the old active set unchanged. Fresh documents keep M1's first-version pointer but cannot
enter product research before readiness. Failed candidate history remains until document purge;
only its retryable stage, not the current version, may be resumed.

Processing a pending version needs an internal run/lease/workspace-authorized original read.
Public original reads keep their current predicates. No external provider call or original download
occurs inside the promotion transaction. Research rechecks its source snapshot before saving;
promotion/deletion races must reject stale context rather than mix old and new outputs.

## 3. Multiple-PDF capture

M2 endpoint: `POST /api/v1/document-imports`, multipart containing one `manifest` JSON part
and file parts named by each item's `client_item_id`. The manifest has `schema_version =
"document-import.v1"` and ordered `items`, each with a unique UUID `client_item_id`, UUID
`request_key`, and existing capture metadata. Reject unmatched/duplicate parts or malformed
manifest before capture. Version uploads and pasted-text batches are not included in this endpoint.

Preflight the entire bounded body into private spools before any canonical capture. Count actual
bytes rather than trusting `Content-Length`. Validate the request envelope globally; then validate
PDF signature/metadata and commit each acceptable item separately. A per-file limit/signature error
rejects that item without hiding accepted siblings. A total-size/count violation rejects the whole
request before capture. Close all spools on disconnect or failure; this is not unlimited buffering.

Return `200` with `schema_version = "document-import-result.v1"` and ordered item outcomes:

```text
ACCEPTED: client_item_id, status, accepted: DocumentAccepted, error: null
REJECTED: client_item_id, status, accepted: null, error: ErrorDetail
```

`200` describes completed admission of the envelope, not completed processing. It may include all
rejected items. Top-level malformed/oversized envelopes use the normal `ErrorResponse`. Per-item
keys permit retrying an interrupted request without duplicating accepted siblings. A successful
replay returns the same accepted identifiers. A transient rejected item may be resubmitted with its
key because no committed capture result exists. Changed input under a committed key conflicts.
If an item's commit result is uncertain, do not emit a definitive `REJECTED` result or delete its
possibly owned original. Resolve by the scoped request key when possible; otherwise stop admission
and return the safe `500 INTERNAL_ERROR` envelope. Previously committed siblings remain captured.
The caller retries the original item keys to discover their committed results without duplication.

## 4. Stages, retries, and public status

Stage types are `PARSE_LEXICAL` and `EMBED_INDEX`, with states `PENDING`, `RUNNING`, `RETRY_WAIT`,
`SUCCEEDED`, and `FAILED`. Disabled dense indexing is represented as `SKIPPED` only in the retained
credential-free lexical profile, never as provider success. Aggregate `ProcessingRun` state retains
the M1 enum. Stage records explain it; clients cannot independently change aggregate readiness.

Each stage has a unique `(workspace_id, processing_run_id, stage_type)`, immutable input/config
identity, output references, and timestamps. A claim creates an append-only attempt identity and
fencing token. Output commit checks current lease owner, source lifecycle, input generation, and
deadline. A stale attempt cannot complete, fail, activate, or overwrite another attempt's output.
Completed stages are not recomputed on duplicate delivery. Retry resumes outstanding index intent
where durable receipts exist; it never blindly assumes a timed-out paid call was unbilled.

Add `GET /api/v1/processing-runs/{run_id}/stages`: ordered stage summaries containing stage ID/type,
state, attempt count/ceiling, next retry time, safe error category/message, and server-derived
`retry_allowed`. Add `POST /api/v1/processing-runs/{run_id}/stages/{stage_id}/retry` with required
request-key header and `expected_attempt_count`; return `202` with run/stage IDs and status URL.
Unknown/foreign IDs are uniformly `404`; active lease, changed expected count, permanent failure,
or exhausted recovery allowance is `409`. A lost retry response is replayable, not another retry.
The existing processing-status endpoint remains unchanged.

Stage retries are allowed for `DEPENDENCY_UNAVAILABLE`, `RATE_LIMITED`, and `TIMEOUT`, including
worker loss only after its fence expires. `INVALID_SOURCE`, `RESOURCE_LIMIT`, `PROVIDER_CONFIGURATION`,
`PROVIDER_CONTRACT`, `SOURCE_INELIGIBLE`, and `BUDGET_EXHAUSTED` do not auto-retry. Budget failures
remain explicit; a new run may be requested after configuration/budget repair, not hidden inside an
unlimited retry. No raw exception, prompt, key, provider response, or storage target enters status.

## 5. Outbox, scheduling, and index convergence

Internal schema: `outbox-event.v1` with `event_id`, `workspace_id`, kind
`PROCESS_DOCUMENT` or `PURGE_DOCUMENT`, target run/document ID, generation, and creation time.
Canonical typed rows hold claim token/expiry, attempt count, next due time, safe last error,
publication time, and state `PENDING`, `CLAIMED`, `RETRY_WAIT`, `PUBLISHED`, `BLOCKED`, or `CANCELLED`.
Enforce unique `(workspace_id, kind, target_id, generation)`; validate the target's scoped FK or
content-free deletion receipt. Step 1 introduces processing delivery only; Step 6 integrates purge.

Publish the existing `processing-job.v1` or `document-purge-job.v1` ID-only payload. Derive event
identity from its unique tuple; never put object keys, original text, endpoints, or credentials in
the event. Delivery uses short ordered `FOR UPDATE SKIP LOCKED` claims; broker publication happens
after commit. Receipt updates require the matching fence. No distributed transaction or exactly-once
delivery claim is made. Broker readiness is checked before consuming an automatic dispatch attempt;
an unavailable broker leaves due intent in PostgreSQL. The outbox publication adapter disables
Celery's hidden publish retries for this path; the canonical claim owns one bounded end-to-end
publication attempt. Connect/write/read work must share its deadline, not each receive a new budget.

One separate Celery Beat process emits repair ticks. Workers execute bounded database scans and
publication; PostgreSQL owns due work and concurrent exclusion. A tick lost in Redis is harmless
because a later tick scans canonical intent again. The scheduler has broker-only credentials, no
database, storage, parser, or AI keys. Reuse the existing dependency/image with a dedicated minimal
entry point, not `worker.py`, which loads worker settings. Schedule-file state is disposable and
must use a writable bounded location, not an application-source mount. No new scheduler package.

Repair also checks published-but-unstarted runs and safely expired execution fences. It records a
bounded new delivery generation rather than erasing old receipts or resetting execution attempts.
Permanent payload/configuration failures or exhausted allowances become visible `BLOCKED` work.
Manual drain/recovery uses the same claims and ceilings; it cannot force a live lease, reset paid
attempt history, expand workspace scope, or treat missing broker state as success.

Index intent identity is `(workspace_id, processing_run_id, chunk_id, embedding_config_version,
dense_index_config_version, operation)`. Keep the existing deterministic provider vector ID.
Receipts distinguish acknowledged writes from verified visibility/absence, including observation
time. Desired vectors include active output and retained unfinished candidates. Purged/ineligible
output is never new evidence, even before stale vectors are removed. Reconciliation uses exact
scoped IDs and bounded pagination; candidate vectors are not orphans merely because not active.

## 6. Initial numeric policy — accepted, not yet runtime settings

The following values derive from current upload/parser, processing, queue, provider, and
deletion bounds and were accepted for M2 on 2026-10-14. Failure tests remain required before
activation. They are not existing new
environment keys or an authorization to change model prices, RAG limits, or the USD 5 monthly cap.

| Policy | Initial contract and derivation |
| --- | --- |
| Batch | At most 4 PDFs, each under the effective existing per-original bound; total original bytes at most 100 MiB and each file never over 100 MiB. Four is the 100 MiB existing configurable upload ceiling divided by the 25 MiB default. Bound manifest JSON to 64 KiB, reusing the existing parser JSON-overhead bound, and remaining multipart overhead to another 64 KiB; hard body cap is 100 MiB + 128 KiB. Enforce actual byte totals and part cardinality before capture. |
| Processing | Inherit 250-page and 2,000,000-character defaults, effective configured limits, and 100,000-character text input ceiling; no silent limit increase. |
| Stage execution | At most 3 automatic claimed attempts and 1 explicitly requested recovery attempt per stage; recovery does not erase previous attempts or repeat a completed stage. Keep 360-second fenced execution budget, with 300/330-second soft/hard task caps inherited from purge; split work into durable bounded batches if it cannot fit. |
| Provider retries | Keep existing 30-second embedding and 20-second Pinecone per-call caps, 30-second visibility deadline, and at most 2 application-owned attempts per operation within the stage deadline. SDK retries stay disabled. Every attempted paid call reserves/accounting independently; delivery generations add no paid allowance. |
| Delivery | At most 3 publication claims per generation and 3 automatic generations per target; maximum automatic publication claims is 9, not 9 processing attempts. Block after exhaustion; do not reset counters each tick. Reuse backoff/jitter with 30-second cap. |
| Scan and claim | At most 20 targets per tick, matching the default catalog page size; 30-second sweep budget, matching the processing-timeout default. Claim individually immediately before publication, with a 30-second fence and 2-second broker-operation cap derived from the readiness cap. Stop scanning/publishing when the deadline is exhausted. |
| Repair | Tick every 30 seconds. A published untouched `CAPTURED` run is eligible for redelivery after 420 seconds, matching broker visibility; running stages require their execution fence to expire, never just publication age. |
| Existing purge | Preserve the 1–100 explicit-ID batch, four scheduled retries, 300/330-second task caps, no restoration, and ADR-0019 retention. Step 6 adds durable global attempt accounting before enabling periodic purge redelivery. |

These are bounds, not latency promises. A long outage or repeated failure can exhaust automatic
recovery; the UI/operations status must say so. A broker outage detected before publication does
not burn processing attempts. Tests must prove eventual recovery after a normal outage and honest
blocked state after exhaustion. Changing a bound requires a versioned policy review.

## 7. Historical evidence access and permanent deletion

Add `GET /api/v1/answers/{answer_id}/evidence-spans/{span_id}` and the same path plus `/original`.
The metadata result retains the existing span/locator fields and adds `answer_id`, `version_number`,
`is_current_version`, and the authorized historical-original URL. Do not expose an arbitrary
inactive-version read or a direct storage URL. The original endpoint verifies the same relationship
and bytes and uses private/no-store responses. Its `viewer_url` is the planned frontend path
`/answers/{answer_id}/sources/{span_id}`; the viewer labels historical sources explicitly.
No answer-list/history screen or conversation feature is added by this contract.

Require a retained `STREAMED` answer, a `VALIDATED` linked statement, an immutable span/block/version
in the same trusted workspace, an `ACTIVE` document, and an integrity-checked original. An invalid
or purged relationship is uniformly `404`. Reprocessing must preserve the old evidence blocks and
offsets; opening history does not rerun a model or recompute a citation against the latest parser.
Authorization and lifecycle are rechecked immediately before preparing a download; already
transferred user-controlled bytes cannot be revoked. New-answer eligibility remains unchanged.

Purge covers every content version, derived output, historical-view link, source-bound duplicate
relation, and complete affected answer. Cancel or detach content-free delivery/index operational
records so delayed tasks cannot recreate removed rows. Preserve detached costs and minimal receipts;
retain only ADR-0021's approved content-free replay tombstones after content purge. A completed
purge still requires exact external absence plus atomic canonical removal, not a broker receipt.

## 8. Migration and verification gate

Append Alembic revisions after `0012_rag_context_bounds`; do not rewrite old migrations or use
application `create_all()`. Capture+outbox writes participate in the same existing unit of work.
Backfill one initial processing event only for eligible unclaimed `CAPTURED` runs, with scoped
unique identity. Do not enqueue terminal, active, or deletion-pending work by migration.
Expired executions are reconciled after startup under their existing fences, not forcibly cleared.

Stage migration records legacy output and readiness with `history_origin = LEGACY_SNAPSHOT` and no
invented attempt rows. Preserve existing `attempt_count` separately as an observed legacy count;
unknown per-stage attempt consumption is not assumed zero or automatically rearmed. Terminal legacy
failures require an explicit reviewed recovery/new-run path. Preserve IDs, bytes, exact spans,
dates, source relationships, and content-free accounting. Schema downgrade must refuse to discard
published/claimed intent, replay protection, irreversible purge history, or detached cost records.

The [synthetic lifecycle catalog](../fixtures/m2/lifecycle-cases.json) specifies expected failures
and invariants, not measured passing tests. Implementation must cover concurrent replay/capture,
promotion/deletion races, partial batch success, unknown/foreign equivalence, broker loss before
and after receipt, worker death, stale fences, partial index visibility, inactive/stale vectors,
historical citations, interrupted purge, and unchanged budgets.

Step 0 records contract acceptance, structurally verified fixtures, migration ownership, and the
bounded implementation sequence; it does not record runtime correctness. Step 1's first block adds delivery contracts,
outbox persistence, scoped constraints, capture-unit-of-work composition, and focused offline tests.
Dispatcher/scheduler/repair and their CI/Compose gates follow in the same numbered step. Preserve
frontend compatibility and run its existing non-browser checks. No paid provider test is implied.
