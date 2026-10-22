# Durable delivery

This module owns content-free publication intent, not processing readiness. Its first M2 block
persists intent with PDF/text capture and preserves it safely through permanent deletion.

| Location | Responsibility |
| --- | --- |
| `domain.py` | Strict versioned event, deterministic identity, fenced transitions, and retry ceilings |
| `contracts.py` | Content-free versioned repair tick with no payload-selected workspace |
| `budget.py` | Shared twenty-slot/thirty-second budget for sequential preparation and publication phases |
| `ports.py` | Separate capture/delivery interfaces and short transaction factory; no broker operations |
| `application.py` | Scoped drain, readiness-before-claim, scan/time bounds, jitter, and safe outcomes |
| `recovery.py` | Bounded snapshots and separately fenced preparation of unstarted/expired redelivery intent |
| `repair.py` | Opt-in preparation/delivery coordinator with one shared budget and reserved publication capacity |
| `persistence.py` | Module-owned `outbox_events` mapping, constraints, and due-work indexes |
| `../../infrastructure/database/outbox.py` | Cross-module canonical target qualification and PostgreSQL repository |
| `../../infrastructure/database/outbox_delivery.py` | Single-row due claims, canonical eligibility, and fenced receipts |
| `../../infrastructure/database/outbox_unit_of_work.py` | Short claim/receipt transactions returning detached immutable values |
| `../../infrastructure/database/unit_of_work.py` | One transaction for document/version/original metadata, processing run, and outbox intent |
| `../../infrastructure/queue/repair_tasks.py` | Validation and registration of the explicitly invoked worker repair task |

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

Generation-zero intent must reference an active current document and an untouched `CAPTURED` run.
Initial intent already handled by the legacy fast path is cancelled as redundant without changing
processing state or execution allowance. Ineligible/deletion-pending sources remain excluded.
Later-generation unfinished execution intent is deferred unless recovery dispatch is explicitly
enabled as described below; it is not cancelled merely because processing previously started.

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
started/terminal work and premature receipts return absence. Exhausted mature publications close
as described in the exhaustion section below. The scoped
unique insert retains previous events. No broker call or normal runtime activation follows from
this operation; expired execution recovery remains a separate work block.

**Bounded recovery scan (2026-10-21):** `RecoverUnstartedOutboxService` reads at most twenty
eligible latest published event IDs, ordered by receipt time and event ID. The scoped query
excludes newer intent in any state, started work, and inactive/non-current
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
`ProcessingRun.can_reclaim_expired`. A changed lease, live lease, completed run, excluded
source, or unavailable lock returns absence. The published-event grace period of 420 seconds is
retained in addition to execution expiry, preventing consecutive scans from immediately spending
all generations while a newly published message waits for a worker. Previous receipts remain
immutable; repeats can reuse the direct successor without resetting it. The operation never
changes execution state, counters, tokens, or outputs and never publishes. This is preparation
only: publication requires the explicit recovery-dispatch qualification described below.
Shared-budget orchestration and automatic activation remain pending. Do not invoke this operation
as a complete recovery workflow.

**Bounded expired-execution scan (2026-10-22):** `list_expired_candidates` shares the scoped
source/latest-event qualification, 420-second grace, receipt/ID ordering, and twenty-target bound
with the unstarted query. It additionally requires unfinished leased parsing/indexing work, an
expired execution fence. Exhausted attempts/generations are included for closure only. Completed
lexical work, including historical M1 completion, is not unfinished indexing. Each content-free
candidate carries the observed event and execution lease IDs. `RecoverExpiredOutboxService`
closes that read transaction before rechecking each observed token through `recover_expired` in
its own transaction. Changed/skipped targets consume a slot. The cooperative thirty-second limit
prevents starting further recovery, not in-flight SQL cancellation. Prepared includes idempotent
successor reuse, not broker delivery or execution. These remain independent preparation services;
the shared-budget runtime coordinator must not concatenate their full budgets into one tick.
Shared-budget orchestration, complete exhaustion status, operator/scheduler wiring, and normal
runtime activation remain pending.

**Opt-in recovery dispatch (2026-10-22):** The delivery transaction factory accepts the existing
processing profile's `maximum_processing_attempts`; its default `None` does not enable expired-run
dispatch. With an explicit ceiling, `claim_next_due` can claim a latest, due later-generation event
for unfinished, expired processing work. It locks the event and run together with `SKIP LOCKED`,
refreshes the canonical snapshot, and qualifies the active/current source under SHARE. Live
executions are filtered before selecting one target, so they do not consume an attempt or hide
other due work. Busy source locks leave intent unchanged for a later sweep. Exhausted execution
allowances block due intent with `ATTEMPTS_EXHAUSTED` without increasing publication counts or
changing the run. Terminal/superseded legacy work remains non-executable. Existing publication
attempt/lease limits still apply. The unchanged `DrainOutboxService` publishes only after the
claim commits/closes; receipts may accept a worker that has already claimed a new execution fence.
This is not an environment option or normal worker wiring. Tests use a recording publisher and
fake consumer against disposable PostgreSQL; real Redis recovery and full runtime gates remain
pending, as do shared-budget tick orchestration and scheduler/operator activation.

**Shared phase budget (2026-10-22):** Both recovery services and the drain accept the same
`OutboxSweepBudget` plus an optional smaller per-call `max_targets` (1–20). The budget is local to
one sequential invocation, not persisted, thread-shared, or authorization. It owns one monotonic
thirty-second deadline and twenty work slots across phases. Preparing and claiming the same target
consume separate slots conservatively; skipped preparations and cancelled/blocked claims count,
while empty lookups do not. Candidate queries receive the remaining, capped `limit` directly in
SQL. Exhausted budget prevents later snapshots, claims, or readiness probes. Broker operations use
its remaining time, not a phase-local reset; in-flight SQL and necessary fenced receipt/failure
cleanup may finish after the deadline. A phase without an explicit budget retains its standalone
limits.

**Opt-in repair coordinator (2026-10-22):** `RepairOutboxService.repair(context)` creates one
budget, prepares at most five unstarted and five expired targets, and drains the remaining slots.
This reserves at least ten work slots for returned delivery claims when both preparations fill
their allowances; empty categories leave more slots available. It does not promise ten successful
publications, hard SQL cancellation, or end-to-end queue fairness. Slow preparation can exhaust
the time budget. Pending preparation during a broker outage consumes no publication attempt;
the drain still checks readiness before claiming. Phase results report only content-free counts
and stop reasons, not completed processing. Unexpected errors propagate. The trusted composition
root must bind the same processing-profile attempt ceiling in the coordinator and delivery
transaction factory. Unit and isolated PostgreSQL tests cover the combined pass and workspace
isolation; no scheduler, command, normal worker wiring, or automatic activation is introduced.

**Recovery exhaustion closure (2026-10-22):** The existing bounded preparation snapshots now
include mature final-generation and execution-exhausted publications for closure, not another
delivery allowance. Under the same active/current source, run, latest-event, and expected execution
fences, targeted recovery marks them `BLOCKED` / `ATTEMPTS_EXHAUSTED`. It preserves the publication
receipt, event identity, generation, publication count, all processing metadata, and older events.
A live execution cannot be closed. Already closed intent leaves later snapshots; repeated repair
does not create new generations or reset attempts. `OutboxRecoveryResult.blocked` counts these
outcomes separately from `prepared`; both consume the existing shared budget. This supersedes
the earlier exhausted-target absence behavior above. Blocked delivery is not proof that an earlier
message was never delivered or that the processing run is terminal.

Migration `0014_outbox_exhaustion_receipts` changes only the publication constraint to permit
an exhausted blocked receipt, retaining all rows/counters. Readiness requires that revision.
Downgrade refuses to discard or mislabel such receipts. No HTTP status/UI, operator command,
scheduler, normal runtime activation, paid call, or new attempt allowance is introduced.

**Explicit worker repair task (2026-10-22):** The worker registers
`investment_research.delivery.repair_outbox.v1` on the existing `document-processing` queue.
Its `outbox-repair-tick.v1` payload contains only the required `schema_version`; extra scope,
content, or policy fields are rejected without echoing input. `ExecuteOutboxRepairService`
resolves the existing server-owned identity and invokes one `RepairOutboxService` pass. Worker
composition binds the same processing-profile ceiling to recovery and delivery and gives the
bounded publisher broker-only settings. Process-local resources remain lazily constructed after
fork and closed with the existing worker container. Repair calls only the existing database/broker
phases; it does not invoke parser, embedding, generation, index, or purge services.

Registration does not send a tick or install a Beat schedule. Results are ignored by Celery,
automatic task retries are disabled, and unexpected failures propagate. The existing cooperative
twenty-slot/thirty-second bounds and short transactions remain unchanged; no new hard SQL/task
cancellation policy is introduced. Duplicate explicitly delivered ticks can run separate bounded
passes, while canonical target claims/ceilings remain authoritative. The queue verifier sends one
explicit tick after its two fixture purges and observes that exact task's completion. That log
observation proves adapter execution, not canonical business-work completion or automatic recovery.

The existing post-commit Celery fast path is still active in the normal app. Bounded repair is now
available through the explicitly delivered worker task, but no capture outbox dispatch, operator
command, scheduler, or periodic sender is enabled. Automatic dispatch and periodic repair remain pending.
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
