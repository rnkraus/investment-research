# Durable delivery

This module owns content-free publication intent, not processing readiness. Its first M2 block
persists intent with PDF/text capture and preserves it safely through permanent deletion.

| Location | Responsibility |
| --- | --- |
| `domain.py` | Strict versioned event, deterministic identity, fenced transitions, and retry ceilings |
| `ports.py` | Separate capture/delivery interfaces and short transaction factory; no broker operations |
| `application.py` | Scoped drain, readiness-before-claim, scan/time bounds, jitter, and safe outcomes |
| `recovery.py` | Bounded snapshot and separately fenced preparation of unstarted redelivery intent |
| `persistence.py` | Module-owned `outbox_events` mapping, constraints, and due-work indexes |
| `../../infrastructure/database/outbox.py` | Cross-module canonical target qualification and PostgreSQL repository |
| `../../infrastructure/database/outbox_delivery.py` | Single-row due claims, canonical eligibility, and fenced receipts |
| `../../infrastructure/database/outbox_unit_of_work.py` | Short claim/receipt transactions returning detached immutable values |
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

## Implemented claim and receipt boundary (2026-10-19)

`claim_next_due` inspects at most one due event in the trusted workspace, ordered by due time
then event ID, with `FOR UPDATE SKIP LOCKED`. It claims individually, not twenty leases ahead
of publication. Concurrent dispatchers skip locked rows. Only a returned `CLAIMED` event may
be published; a returned `CANCELLED`/`BLOCKED` event still consumes one of the future caller's
twenty scan slots. `None` means no unlocked due event, not proof that all work is complete.

The event must reference an active current document and an untouched `CAPTURED` run. Intent for
a run already started by the legacy fast path is cancelled as redundant without changing its
processing state or execution allowance. This also applies to ineligible/deletion-pending sources.
Recovery of expired execution leases belongs to the later reconciliation block, not this claim.

Each claim increments only the event's publication count and receives a fresh 30-second token.
Only expired delivery claims can be reclaimed. A third expired claim becomes `BLOCKED` rather
than receiving a fourth attempt. A transient publication failure schedules a bounded retry
(caller-selected delay/jitter at most 30 seconds); its third attempt also blocks. Invalid jobs
block immediately. Cancellation preserves prior publication history and never resets a run.

Receipt writes require the same workspace, event, token, and unexpired lease; a receipt exactly
at expiry is rejected even before another dispatcher takes over. Deletion cancellation wins over
late receipts. Receipts recheck source lifecycle/current version but allow an already-started
worker: a successful publish may start processing before its receipt is recorded. Publication
acknowledgement is never processing completion. The worker remains the execution authority,
including eligibility checks for publication/deletion races.

Repositories never commit. The typed delivery unit of work commits and closes each claim before
the future broker adapter receives its detached value, and opens a separate receipt transaction.
No broker call is part of these database operations. The drain checks broker readiness before
claiming and enforces the sweep, publication deadline, and scan bounds.

## Implemented bounded publication boundary (2026-10-19)

`DrainOutboxService` checks readiness before each claim, then commits/closes the claim transaction,
publishes the existing ID-only `ProcessingJobV1`, and records the outcome in a separate transaction.
It inspects at most 20 returned rows in a 30-second monotonic sweep; cancelled/blocked rows count
toward that limit. The next publication receives the minimum of the two-second broker cap, sweep
time remaining, and delivery-lease time remaining. It never claims a batch ahead of sending.

A known broker outage before claiming consumes no attempt. A timeout/lost acknowledgement after
claiming is uncertain delivery: persist a safe transient failure and stop this sweep, rather than
burning the rest of the workspace's attempts. Retry delay uses capped exponential full jitter;
the existing per-generation ceiling still applies. Invalid publication blocks the owned event;
unexpected programming/database errors propagate instead of being mislabeled as broker outages.
An expired or cancelled receipt is counted separately, not reported as acknowledged publication.

`infrastructure/queue/outbox.py` enforces one wall-clock broker budget using a disposable subprocess,
including helper startup, Celery connection/write/read, and acknowledgement. It kills/reaps a timed
out helper and rejects a late success. OS scheduling/reaping can add cleanup latency; the deadline
is not a real-time latency guarantee. This is an internal adapter, not a new service or a parser
sandbox. It works from prefork workers without Python's daemon-child multiprocessing restriction.

The helper receives only broker URL, runtime location, deadline, and optional typed ID-only job
through a bounded private stdin pipe. Credentials never appear in argv. Database/storage/provider
environment variables are not inherited; standard output/error are discarded. No settings loader
or environment/secret file is read in the helper. Its result is a safe exit category, not a raw
SDK exception. Celery/Kombu connection and publication retries are disabled for this path. A killed
publisher may already have queued its message; at-least-once execution fences remain authoritative.

The queue-recovery verifier defers the synthetic capture's legacy dispatch and drains its canonical
intent through this adapter before the existing worker-loss/recovery checks. This is provider-free
CI coverage of real Redis delivery, not production activation or full periodic repair.

## Deliberately pending

**Recovery value rule (2026-10-20):** `OutboxEvent.redeliver_unstarted` can derive the next
deterministic generation only from `PUBLISHED`, at least 420 seconds after its receipt, and below
the three-generation ceiling. The previous immutable event/receipt is unchanged. This pure rule
does not inspect or authorize canonical work. Persistence must lock/recheck the scoped
active/current, unstarted run and latest event, retain prior rows, and insert idempotently under
scoped uniqueness; repeated scans must not overwrite an existing generation's attempts or receipts.
It cannot revive blocked work or authorize a source merely from the passage of time.

**Targeted recovery persistence (2026-10-21):** `recover_unstarted` applies that rule in the
caller's transaction. It checks trusted workspace and active/current source under a document SHARE
lock, locks only an unstarted captured run, then locks its exact latest event. Locked targets are
skipped, never replaced by an older unlocked generation. SHARE is compatible with processing's
source-write guard; it still fences deletion/promotion. A repeat can return its existing direct
successor without resetting attempts/receipts. Older superseded requests, foreign/unknown IDs,
started/terminal work, premature receipts, and exhausted generations return absence. The scoped
unique insert retains previous events. No broker call or normal runtime activation follows from
this operation; expired execution recovery remains a separate work block.

**Bounded recovery scan (2026-10-21):** `RecoverUnstartedOutboxService` reads at most twenty
eligible latest published event IDs, ordered by receipt time and event ID. The scoped query
excludes newer intent in any state, exhausted generations, started work, and inactive/non-current
sources. It waits the same 420 seconds from publication. The snapshot transaction closes before
each separate `recover_unstarted` transaction rechecks eligibility and locks. Skipped candidates
consume a scan slot; repeats never reset attempts or receipts. Content-free results count scanned
targets and prepared successors, including an idempotently reused successor, not new executions.
The thirty-second cooperative deadline stops further transactions; it is not an SQL cancellation
guarantee. This service neither publishes nor combines two independent thirty-second budgets into
one scheduler tick. A shared-budget runtime coordinator, expired-execution recovery, exhausted-work
status, operator command, and scheduler activation remain pending.

**Targeted expired-execution preparation (2026-10-22):** `recover_expired` additionally accepts
the expected execution lease ID and the existing processing profile's attempt ceiling. Under the
same source SHARE/run/latest-event locks, it refreshes the run and applies
`ProcessingRun.can_reclaim_expired`. A changed lease, live lease, completed/exhausted run, excluded
source, or unavailable lock returns absence. The published-event grace period of 420 seconds is
retained in addition to execution expiry, preventing consecutive scans from immediately spending
all generations while a newly published message waits for a worker. Previous receipts remain
immutable; repeats can reuse the direct successor without resetting it. The operation never
changes execution state, counters, tokens, or outputs and never publishes. This is preparation
only: the existing captured-only drain cannot yet dispatch this intent. Expired-work scanning,
appropriate drain qualification, shared-budget orchestration, and automatic activation remain
pending. Do not invoke this operation as a complete recovery workflow.

The existing post-commit Celery fast path is still active in the normal app. The drain and adapter
are exercised explicitly in verification, but not wired into capture, worker bootstrap, an operator
command, or a scheduler. Automatic dispatch, scheduler, and periodic repair remain pending.
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
