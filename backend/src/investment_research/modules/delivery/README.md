# Durable delivery

This module owns content-free publication intent, not processing readiness. Its first M2 block
persists intent with PDF/text capture and preserves it safely through permanent deletion.

| Location | Responsibility |
| --- | --- |
| `domain.py` | Strict versioned event, bounded delivery metadata, and deterministic identity |
| `ports.py` | Scoped persistence interface; no independent commits or broker operations |
| `persistence.py` | Module-owned `outbox_events` mapping, constraints, and due-work indexes |
| `../../infrastructure/database/outbox.py` | Cross-module canonical target qualification and PostgreSQL repository |
| `../../infrastructure/database/unit_of_work.py` | One transaction for document/version/original metadata, processing run, and outbox intent |

## Implemented storage boundary

`OutboxEvent.for_processing` creates generation zero for one canonical run. Its UUIDv5 uses
`NAMESPACE_URL` and the stable name
`investment-research:outbox-event.v1:{workspace_id}:PROCESS_DOCUMENT:{run_id}:{generation}`.
Creation time is stored separately and never changes event identity. Uniqueness covers workspace,
kind, target, and generation; repeat writes return the retained event without resetting its state.

The infrastructure repository qualifies the run through its active canonical document and trusted
workspace and locks that document in the caller's transaction. Scoped foreign keys and checks
provide database defense in depth. It never commits or contacts Redis. Unknown and foreign reads
both return absence; ineligible intent writes share one safe error category.

Capture writes the event in the same transaction as the run. A normal pre-commit failure rolls
back all rows and compensates only the uncommitted object. An uncertain commit outcome preserves
the possibly canonical original and produces a safe API error instead of deleting it. This does
not yet implement request replay, which belongs to M2 Step 3.

Canonical permanent purge cancels and detaches matching outbox rows in its own transaction before
removing runs. Retained IDs, counts, safe categories, and publication times contain no source text,
title, object key, model output, or credential. Published history is preserved; cancellation and
publication never prove that parsing/indexing or external purge completed.

Migration `0013_processing_outbox` backfills only active-current, unclaimed `CAPTURED` runs. It
does not reset leases, revive failed work, or manufacture execution attempts. Downgrade refuses to
discard any retained delivery intent/history; the empty-schema migration cycle remains supported.

## Deliberately pending

The existing post-commit Celery fast path is still active. No outbox dispatcher, claim operation,
scheduler, periodic repair, or publication receipt writer is enabled in this storage block.
Consequently `PENDING` means no outbox-owned publication receipt, not proof the legacy fast path
never published or the worker never ran. The later dispatcher must recheck canonical readiness
and lifecycle before claiming or publishing this intent. Duplicate delivery remains safe under the
existing execution lease.

`PROCESS_DOCUMENT` is the only currently enabled outbox kind. `PURGE_DOCUMENT` remains the reserved
M2 Step 6 extension; current purge tasks still use their existing delivery adapter. Retry/history,
source versions, historical reads, and capture-key tombstones retain their owning M2 steps.

See the accepted [M2 contracts](../../../../../docs/15-m2-contracts.md) and
[implementation plan](../../../../../docs/12-m2-implementation-plan.md). Existing backend unit and
PostgreSQL verifier discovery includes the new contract, transaction, schema, and migration tests;
no new paid-provider CI gate is introduced.
