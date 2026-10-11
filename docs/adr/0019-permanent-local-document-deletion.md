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
place Delete selected above the catalog, next to the other selection actions. Initially refuse a
batch containing a still-processing document rather than racing a worker; do not silently delete
only part of that batch. Never use a bucket-wide or namespace-wide delete.

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
synthetic PostgreSQL and local storage, and adapter tests use provider doubles. They do not prove
live Pinecone or SeaweedFS cleanup. Task registration, dispatch, the HTTP endpoint, and website
activation remain pending; this is not yet a usable deletion feature.

Backend-block verification (2026-10-12): `scripts.verify_backend` passed formatting, lint, typing,
and 2,039 unit tests. `scripts.verify_postgres` passed the empty migration cycle, schema checks,
and 215 integration tests; seven SeaweedFS tests were skipped because their separate verifier was
not active. Frontend format, lint, typecheck, contract checks, and 61 tests passed. No browser,
production build, live provider deletion, or normal archive purge was performed for this block.
