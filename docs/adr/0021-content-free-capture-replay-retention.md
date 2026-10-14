# ADR-0021 — Content-free capture replay protection after purge

**Status:** Accepted for M2 implementation — owner approved on 2026-10-14; not yet implemented.

**Scope:** A narrow supplement to [ADR-0019](./0019-permanent-local-document-deletion.md).
Its permanent source/answer removal, exact-target cleanup, minimal deletion receipt, and detached
accounting remain unchanged. This decision adds a separate content-free replay record; it does
not retain a deleted capture response or authorize restoration.

## Context and decision drivers

M2 request keys distinguish repeated delivery of one capture from an intentional new import.
Deleting all replay information would let a delayed client retry recreate a source after its
permanent purge. Keeping the original replay response or content fingerprint would instead retain
source-bound metadata that deletion should remove. The owner approved retaining only a
content-free used-key marker.

## Options considered

1. Remove replay protection on purge: rejected because a delayed retry can create another document.
2. Retain the full capture response/fingerprint: rejected because it preserves unnecessary deleted
   metadata and could return links to unavailable content.
3. Retain a minimal scoped used-key marker: selected. It prevents replay resurrection while a
   separately keyed intentional import remains possible.

## Decision

During purge, atomically replace live replay outcomes with a tombstone containing only trusted
workspace ID, operation discriminator, optional target document UUID, request UUID, and UTC
tombstone time. Retain no title, filename, text, checksum/fingerprint, object key, provider output,
answer, original/version/run response, or unbounded client-supplied string.

The operation/target/request identity remains scoped to its workspace. Check it before creating
new content or publishing work. Reusing a tombstoned key returns the safe `409 CONFLICT` outcome;
it neither restores content nor returns its old accepted response. A new request key is a new
intentional import, subject to ordinary admission, not an exception to the tombstone.

Keep the marker for as long as the workspace's deletion receipts. No time-based retry sweep may
expire it, reset it, or treat it as permission to reconstruct a source. Full workspace retirement
and any public/multi-user retention policy need a separately reviewed decision. Identifiers remain
metadata, not anonymous data or authorization proof; do not log complete replay records by default.

## Consequences and validation

Minimal operation metadata survives content purge, requiring storage and safe schema-downgrade
handling. Original/source/answer data still disappears under ADR-0019. The marker does not change
cost accounting, permit broad deletion, or retract provider-held/user-exported copies.

Before activation, test concurrent capture/purge, retry after uncertain commit, same-key reuse after
purge, independent new-key import, workspace non-disclosure, absence of content/fingerprints in
retained rows, rollback atomicity, and refusal to downgrade away replay protection. Use disposable
public-synthetic resources only. The accepted [M2 contracts](../15-m2-contracts.md) define the
surrounding request, version, and failure behavior; this ADR does not claim those tests passed.
