# ADR-0019 — Permanent local document and answer deletion

**Status:** Accepted — owner approved; implementation in progress

**Date:** 2026-10-12

**Scope:** Local archive selection and permanent deletion before the broader M2 lifecycle work.
This decision supersedes the draft 30-day restoration policy for this local deletion path; it does
not approve public deployment or declare the deletion feature implemented.

## Context and decision drivers

The owner requested deletion of one or several saved documents, explicitly declined restoration,
and approved deleting complete stored answers that used those documents, including answers that
also cite retained documents. Removing a catalog row alone would leave source copies and vectors.
Deleting accounting would incorrectly restore the monthly provider budget. Separate stores cannot
commit one distributed transaction, and immutable originals must remain protected during normal use.

## Options considered

1. Hide rows only: rejected because it leaves usable evidence and source copies.
2. Soft-delete with 30-day restoration: declined by the owner for the local application.
3. Permanently exclude first, then idempotently purge exact scoped targets: selected.

## Decision

Accept only explicit batches of 1–100 unique document IDs under the trusted `WorkspaceContext`.
An empty batch never means all documents. Confirm permanent deletion in the UI before submission;
place Delete selected above the catalog, next to the other selection actions. Refuse a batch
containing started parsing or indexing work rather than racing a worker; do not silently delete
only part of that batch. Never use a bucket-wide or namespace-wide delete.

**Owner-requested waiting-source extension (2026-10-12):** An unclaimed `CAPTURED` run is deletable:
its canonical state requires zero attempts, no start time, and no lease. Admission locks the document;
the existing source-write guard serializes a competing claim and rejects a delayed claim after
exclusion, before processing can open storage or invoke the parser. Completed `LEXICAL_READY` runs
with no lease are deletable regardless of their embedding/index configuration, including historical
configurations. Configuration labels are not proof of active execution; completion and absent lease
metadata are required. Exact deterministic vectors are still purged for configurations other than
the current disabled-embedding sentinel. A leased `LEXICAL_READY`, `PARSING`, or `INDEXING` run still
rejects the entire batch, even if its lease expired: expiry alone does not prove external work has
stopped. This is not active-worker cancellation or stale-run recovery.

Commit `Document.lifecycle_state = PURGE_PENDING` and a content-free `document_deletions` receipt
together. Pending deletion is already irreversible: normal listing, retrieval, original opening,
and new answer admission must exclude it. There is no restore endpoint. External cleanup runs
outside database transactions and uses only canonical keys and deterministic vector IDs for every
version and processing output of the selected document. Provider failures leave durable pending
intent for retry, never an active document or a false completion message.

Only finalize the receipt as `COMPLETED` after external cleanup is confirmed and the PostgreSQL
purge commits. Duplicate delivery and repeated deletion must have the same result. Concurrent
processing/research must not recreate purged content or save an answer against deleted evidence.
Keep normal immutable-update protection; any delete-only exception must verify the pending
document and its scoped deletion receipt, and must be tested before enabling physical purge.

### Retention matrix

| Data | Permanent-deletion treatment |
| --- | --- |
| Original objects for all document versions | Delete exact workspace-scoped object keys; repeated deletion is harmless. |
| Pinecone vectors for all derived chunk sets | Delete exact deterministic IDs in the workspace namespace and verify absence; no namespace clearing. |
| Versions, original metadata, processing outputs, chunks, runs | Physically remove after external cleanup, respecting FK dependencies and narrowly guarded immutability. |
| Research requests exposed to a deleted source | Remove when their explicit filter, retrieval candidates, or statement evidence relates to that source. |
| Answers for affected requests | Remove complete answers, all their statements, citation relationships, validations, and retrieval history, including multi-document answers. |
| Other documents and unrelated answers | Preserve; never expand document deletion to the workspace. |
| Provider accounting | Preserve content-free usage, reservations, costs, and timing; detach deleted request/run FKs using `owner_deleted = true`. Budget accounting still counts them. |
| Deletion receipt | Retain only workspace/document IDs, state, request time, and completion time. No titles, source text, object keys, or model output. |
| Backups and previously exported/downloaded copies | Not claimed erased by an application deletion; the existing backup-expiry boundary remains. |

Do not imply that the application can retract source text already sent to external AI providers or
erase user-controlled terminal output, exported reports, downloads, or backups. Default logs/traces
remain content-free; opt-in local raw-output diagnostics and provider retention require separate
operator handling. No claim of immediate erasure of every external copy is made.

## Consequences and validation

There is no application recovery action after confirmation. Pending versus completed cleanup must
be visible and honest. Completed receipts and detached accounting make rollback to an older schema
unsafe; downgrade must refuse to discard them. M2 inherits this local policy and still owns its
broader durable delivery, reconciliation, and lifecycle guarantees.

Before activation, prove bounded admission, unknown/foreign non-disclosure, all-or-nothing intent,
immediate evidence denial, ongoing-work rejection, full dependent-answer cleanup, preserved costs,
exact-key/vector isolation, repeated/failed/interrupted cleanup, immutable-source protection, and
clean-database plus existing-history migrations. Use only disposable synthetic resources. Public
or multi-user operation requires a separately reviewed retention and authorization policy.

The backend now includes admission and purge services, canonical cleanup manifests, exact-key
storage and exact-ID vector adapters, and atomic dependency cleanup. Migration
`0011_document_purge_guards` permits delete-only cleanup of pending originals/versions and rejects
source-bound writes after exclusion. Shared document locks serialize those writes with deletion
admission. In-flight provider accounting may finalize after its deleted owner is detached, without
reattaching it or resetting the budget.

The S3 adapter targets the current unversioned local SeaweedFS store; versioned cloud object
history requires a separately reviewed purge policy. Backend integration tests use disposable
synthetic PostgreSQL and local storage, and adapter tests use provider doubles. The worker now
registers a versioned ID-only purge task on the existing queue, checks the trusted workspace, and
reuses its process-local storage/vector clients. Pending errors receive four scheduled retries
with backoff and jitter; attempts have 300/330-second soft/hard limits. Worker-loss redelivery is
separate. Exhausted cleanup remains canonically pending, not completed or restored. This does
not implement M2's durable outbox or periodic reconciliation.

The isolated queue-recovery verifier publishes deletion jobs after recovering its synthetic source
and confirms a completed receipt and absent canonical source rows. The service confirms exact
original-object absence in live unversioned SeaweedFS before completion. Confirmed batch admission
and scoped receipt reads are now exposed through the
[HTTP contract](../11-m1-contracts.md#34-permanent-deletion-follow-up). Admission commits before
publication; failed publication IDs are returned without restoring content, and resubmission
retries pending jobs. Status snapshots never expose source text or provider/storage targets.
The website now implements above-catalog multi-document confirmation, honest cleanup status,
bounded receipt reads, and explicit same-batch retry. Failed or unprocessed sources are deletable
but never admitted to research. Queued and Text extracted sources are also selectable for deletion;
the backend rejects Text extracted if its run is unfinished or still owns a lease. Parsing and Indexing
remain unselectable. A deletion attempt clears
the current answer view and refreshes the catalog, including uncertain commit outcomes. Tracking
is page-local and the reload limitation is shown explicitly; accepted worker cleanup continues.
Manual browser/build verification and live Pinecone deletion verification remain pending.

Backend-block verification (2026-10-12): `scripts.verify_backend` passed formatting, lint, typing,
and 2,039 unit tests. `scripts.verify_postgres` passed the empty migration cycle, schema checks,
and 215 integration tests; seven SeaweedFS tests were skipped because their separate verifier was
not active. Frontend format, lint, typecheck, contract checks, and 61 tests passed. No browser,
production build, live provider deletion, or normal archive purge was performed for this block.

Worker-block verification (2026-10-12): `scripts.verify_backend` passed formatting, lint, typing,
and 2,064 unit tests. The isolated `scripts.verify_queue_recovery` gate passed recovery and
permanent PostgreSQL/SeaweedFS cleanup; it published the same purge job twice. Unit tests cover
duplicate execution, retry exhaustion, invalid/foreign payloads, soft timeouts, client wiring,
and deterministic-index exact-ID isolation. Frontend format, lint, typecheck, contract checks, and
61 tests also passed. No browser or production build was run for this backend-only block. This
gate does not exercise live Pinecone deletion or the user's normal archive.

API-block verification (2026-10-12): `scripts.verify_backend` passed formatting, lint, typing,
and 2,093 unit tests. `scripts.verify_postgres` passed migration cycling, schema checks, and 219
integration tests; seven separate SeaweedFS tests were skipped. API tests verify post-commit
publication using a separate connection, broker failure/resubmission, whole-batch rejection, and
receipt visibility/isolation after physical removal. OpenAPI drift checking and frontend format,
lint, typecheck, generated-contract checks, and 61 tests passed. No browser, production build,
live Pinecone deletion, or normal archive deletion was performed for this block. UI activation
was the next work block at that verification point.

Frontend-block verification (2026-10-12): `pnpm format`, `pnpm lint`, `pnpm typecheck`, and
`pnpm test` passed, including generated-contract checks and 75 deterministic tests. Two mocked
deletion browser tests were added to the existing CI discovery and type-checked, not executed
locally. No browser, production build, live Pinecone deletion, or normal archive deletion was
performed. The backend was unchanged in this block; its earlier verification remains separate.

Waiting-source extension verification (2026-10-12): backend format, lint, typing, and 2,093 unit
tests passed. The isolated PostgreSQL gate passed empty migration cycling, schema checks, and 227
integration tests; seven separate SeaweedFS tests were skipped. Added regressions cover queued-source
purge, delayed-claim fencing, competing claim/admission in both lock orders, whole-batch rejection,
and refusal of started work even with expired leases. Frontend format, lint, typing, contract checks,
and 83 deterministic tests passed. Additional mocked browser cases were type-checked, not executed
locally. No production build, live provider deletion, or normal archive purge was performed.

Historical completed-source correction (2026-10-12): deletion admission now checks completion and
absent lease metadata instead of requiring the current disabled-embedding configuration label.
Backend formatting, lint, typing, and 2,093 unit tests passed; the changed integration module also
passed typing with the application sources. The isolated PostgreSQL gate passed migration cycling,
schema checks, and 229 integration tests; seven separate SeaweedFS tests were skipped. Regressions
prove complete historical-source/answer cleanup, exact vector targets, idempotency, and stale-write
rejection while leased work remains protected. Frontend documentation formatting passed. No normal
archive deletion, live provider cleanup, browser, or production build was performed for this fix.
