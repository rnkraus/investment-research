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
| `dispatch.py` | Normal API post-capture adapter resolving trusted scope and delivering only existing generation-zero intent |
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

## Targeted single-event claim (2026-10-23)

`claim_due_event` narrows the same database claim implementation to one explicit event ID in the
trusted workspace. It never substitutes another due row when that event is absent, foreign,
not yet due, superseded, already published/closed, or locked. The existing latest-generation,
active/current source, deletion, execution, profile, lease, retry, and attempt rules remain shared
with `claim_next_due`; a returned `CANCELLED`/`BLOCKED` result never authorizes publication.
Both operations use single-row `SKIP LOCKED` claims and caller-owned short transactions. Rollback
preserves intent, and concurrent exact claims have at most one owner without claiming unrelated
work. Neither operation contacts the broker, changes processing attempts, or creates intent.

This is the persistence prerequisite for a bounded post-capture fast path, not its activation.
The caller must still check broker readiness before claiming, commit/close before publication,
and retain existing fenced receipt handling afterward. The normal API dispatcher, worker,
scheduler, schemas, and capture response are unchanged by this block. Existing unit/PostgreSQL
CI discovery covers query scope, exact selection, concurrency, rollback, source exclusion,
due times, exhaustion, and explicit recovery-profile qualification.

## Targeted post-capture dispatch (2026-10-23)

`DrainOutboxService.deliver_event` visits at most one exact event through `claim_due_event` and
shares the sweep's existing readiness, cooperative deadline, broker caps, jitter, publication,
and receipt implementation. No unrelated due row is substituted or drained. Readiness precedes
the short claim transaction; it closes before the single broker publication and separate fenced
receipt transaction. Known broker unavailability consumes no attempt before a claim; uncertain
publication records bounded retry intent, invalid publication blocks the owned event, and a late
or rejected receipt is not counted as acknowledged success. This is not a hard SQL timeout or
new end-to-end latency guarantee. Unexpected programming/database/commit errors still propagate.

`DispatchCapturedProcessingService` implements the existing `ProcessingDispatcher` boundary for
normal API capture. It revalidates the job, resolves the server-owned identity, rejects a
workspace mismatch, and derives only that run's deterministic generation-zero event ID. It never
creates intent, selects a recovery generation, resets attempts, falls back to direct Celery
publication, or retries inside capture. Expected delivery failures use `ProcessingDispatchError`,
which capture already handles after commit without compensating its canonical original. An empty
claim is an idempotent no-op, not proof of delivery, processing completion, or source eligibility.

The normal API composition now shares its trusted identity provider with this adapter and passes
the selected processing profile's attempt ceiling to its delivery transactions. Both explicit
service tests and HTTP PDF/text capture tests exercise disposable PostgreSQL with a deterministic
publisher, including retained originals, unchanged unrelated due intent, and no open transaction
during publication. Existing unit/PostgreSQL CI discovers these tests. No schema, settings, HTTP
response, provider, or frontend contract changes. The base Compose stack now starts periodic repair
with the API; the isolated HTTP outage gate below covers automatic recovery of captured work.

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

## Explicit local operator command (2026-10-22)

From `backend/`, run exactly one repair pass:

```shell
uv run --locked python -m investment_research.repair_outbox
```

The command loads the existing database/broker connection configuration and
`INVESTMENT_RESEARCH_PROCESSING_PROFILE` (default `m1`), not storage/parser/provider settings or
the full worker bootstrap. In containers it supports the existing mounted database/Redis secret
files. It requires the current Alembic revision before mutation and closes its engine on success
and failure. Scope comes from the same fixed local identity, never arguments. There are no force,
scope, counter-reset, limit-override, or loop flags. `--help` requires no configuration/connection.

This invokes the existing coordinator directly; it needs PostgreSQL and Redis but not a working
scheduler or repair-tick consumer. Processing still requires a worker. The publisher receives only
broker settings and preserves existing deadlines/fences/ceilings. The command itself performs no
parser, storage, or AI calls; published jobs may invoke paid providers in a configured live worker,
subject to its unchanged budget and attempt rules. Do not treat invocation as a read-only check.

Output contains only phase counts and stop categories. Exit `0` means one bounded pass completed,
not an empty queue, healthy archive, or completed document processing. `TARGET_LIMIT` can leave work
for another invocation. Exit `1` means configuration/database/execution/cleanup failure, a deadline,
unavailable/invalid publication, rejected receipts, or exhausted work encountered in this pass.
There is no automatic command retry. Previously blocked rows outside this pass are not summarized.
Failure details and connection URLs are withheld. The queue verifier runs the command against the
empty isolated archive after both fixture purges and checks that their canonical absence remains.

## Separate scheduler implementation (2026-10-22)

`investment_research.scheduler` is the dedicated Celery Beat entry point, not a worker import or
embedded worker scheduler. Its settings loader reads only runtime and broker process values; it
does not load a local environment file or resolve database/storage/provider configuration. The
application factory installs exactly one thirty-second `outbox-repair-tick.v1` schedule. Creation
alone neither connects nor sends. The normal API/worker factories do not install this schedule.

`infrastructure/queue/scheduler.py` retains Beat's interval/state handling but bypasses its default
producer and raw publication-error reporting. Each due entry must match the fixed task, empty
arguments/options, and exact content-free payload. The existing private subprocess publisher owns
the same two-second send deadline and disabled retries. The helper generates the fixed signal;
callers cannot supply document/workspace IDs or another task. Unstarted ticks expire after thirty
seconds; this does not stop an already running repair pass or alter canonical business intent.
Unavailable/invalid publication emits only a static event and waits for a later interval. An
unexpected failure stops the scheduler through a sanitized error boundary. Missed ticks do not
erase due work, consume canonical delivery attempts, or provide paid-operation allowance.

Beat state uses `/tmp/investment-research-beat-schedule`, never application sources or business
storage. SIGTERM follows Beat's shutdown/sync path; the Celery application closes on exit. Exactly
one scheduler process is required. Compose must still give that process broker-only secrets,
restricted networking, non-root/read-only execution, and bounded writable temporary storage before
normal activation. No Compose service or recurring normal-runtime sender is enabled in this block.
The queue verifier exercises one due Beat step with disposable schedule state and the real bounded
Redis sender, checks no immediate repeat, and retains canonical absence after synthetic purges.
It does not prove a separately running periodic process or automatic outage recovery end to end.

## Default Compose scheduler (2026-10-23)

`compose.yaml` defines one scheduler using the existing worker image with the
dedicated Python entry point and the inherited worker health check disabled. It receives only the
Redis password secret and five runtime/broker environment values, with no ports, source/config
mounts, database/storage/provider credentials, or Docker socket. A dedicated internal `repair`
network connects only the scheduler and Redis; Redis also retains its existing backend network.
The service runs as `65532:65532`, with read-only root, all capabilities dropped,
`no-new-privileges`, a private 16 MiB non-executable `/tmp`, and 256 MiB/0.5 CPU/64-PID limits.
Do not scale it above one. There is no worker-style ping health check: running-container state
does not prove periodic publication, worker availability, or completed business work.

API startup includes that scheduler; worker-only commands must name it explicitly when periodic
repair is required. `compose.m2-repair.yaml` remains a no-op compatibility overlay and cannot create
a second scheduler. The queue verifier starts the API/scheduler only after its controlled M0
worker-loss fixtures are purged, using the existing M1 acceptance override for both PDF and text.
It checks actual container controls/secret mounts, internal networking, blocked PostgreSQL
IP/parser/storage/external connections, and permitted authenticated Redis access. It waits for two
autonomous publications at the unchanged interval, stops Redis, requires a safe unavailable event,
restarts Redis even when the outage observation fails, and requires a new successful publication.
The same scheduler container/restart count must remain; canonical source absence is checked again.
Logs/inspection JSON are captured rather than printed. The shared project is removed on failure too.

The same isolated gate then stops Redis and uploads PDF and text through the running API. Both
must return `CAPTURED`, retain byte-exact originals, and leave generation-zero intent pending with
zero delivery/execution attempts throughout the outage. After Redis returns, only the existing
periodic scheduler may initiate recovery: both runs must reach `SEARCHABLE`, with one publication,
one processing attempt, and one canonical chunk set each. Another scheduled tick must leave their
output IDs unchanged. API, worker, and scheduler IDs/restart counts must not change. The probe
never republishes jobs, repairs database values, shortens grace, or restarts those processes.

Normal startup now deliberately enables canonical repair scans and can enqueue existing eligible
processing work; live workers retain their normal provider/budget and exhaustion rules. Neither
running the scheduler nor acknowledging publication proves business completion. No new secrets,
provider keys, schema, RAG settings, or paid test calls are required. The legacy direct processing
publisher is not the normal API path; duplicate delivery remains safe under the execution lease.

`PROCESS_DOCUMENT` is the only currently enabled outbox kind. `PURGE_DOCUMENT` remains the reserved
M2 Step 6 extension; current purge tasks still use their existing delivery adapter. Retry/history,
source versions, historical reads, and capture-key tombstones retain their owning M2 steps.

See the accepted [M2 contracts](../../../../../docs/15-m2-contracts.md) and
[implementation plan](../../../../../docs/12-m2-implementation-plan.md). Existing backend unit and
PostgreSQL verifier discovery includes the new contract, transaction, schema, and migration tests;
no new paid-provider CI gate is introduced.
