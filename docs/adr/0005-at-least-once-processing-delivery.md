# ADR-0005 — At-least-once processing delivery and execution lease

**Status:** Accepted

**Date:** 2026-09-13

**Scope:** M0 Redis/Celery delivery of canonical document-processing runs

## Context

M0.0 creates a canonical `ProcessingRun` in PostgreSQL and executes it once through a manual local
runner. M0 replaces manual invocation with Redis and Celery. Celery may deliver the same task more
than once, especially when a worker exits before late acknowledgement. Redis may also be unavailable
after document capture has committed.

The current processing service commits `CAPTURED → PARSING` before it opens and parses the original.
This keeps a long parser operation outside a database transaction, but a worker crash can leave the
run in `PARSING`. A redelivered task must distinguish that abandoned attempt from a concurrent worker
that is still processing it.

M2 will add the general `Job`, `ProcessingStage`, transactional-outbox, and reconciliation model. M0
needs a smaller mechanism that makes its one processing task safe without prematurely implementing
that complete lifecycle.

Celery documents that late acknowledgement permits redelivery and therefore requires idempotent
tasks. It also warns that requeueing work after worker loss can create message loops. The application
must not rely on a Celery task identifier or result backend as its correctness boundary.

## Decision drivers

- keep PostgreSQL as the canonical processing-state owner;
- make duplicate and worker-loss redelivery safe;
- avoid holding a database transaction or row lock while parsing;
- keep queue payloads small, typed, versioned, and free of document content or secrets;
- bound attempts and prevent infinite poison-message loops;
- preserve a visible recovery path when Redis publication fails;
- avoid implementing the broader M2 job and outbox model early.

## Options considered

1. **Acknowledge before execution and accept lost work.** Simple, but a worker crash can leave a
   canonical run permanently unprocessed without redelivery.
2. **Use late acknowledgement without an application lease.** Redelivers work, but cannot safely
   distinguish an abandoned `PARSING` run from concurrent execution.
3. **Hold a PostgreSQL transaction and row lock for the complete parse.** Prevents concurrency but
   keeps a database connection and transaction open across untrusted, bounded CPU work.
4. **Use a Redis lock as the execution authority.** Fast, but Redis is transient and cannot explain
   canonical state after loss or restart.
5. **Add a short canonical lease and attempt counter to `ProcessingRun`.** Allows a worker to claim in
   a short transaction, execute without an open transaction, and recover an expired claim.
6. **Implement the full M2 Job/Stage/outbox model now.** More general, but expands M0 beyond the
   smallest foundation needed for one processing workflow.

## Decision

M0 adopts option 5 with at-least-once Celery delivery.

### Versioned queue payload

The application contract is a strict value object equivalent to:

```python
class ProcessingJobV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["processing-job.v1"]
    workspace_id: UUID
    processing_run_id: UUID
    idempotency_key: str
```

The deterministic idempotency key is derived from the schema version, workspace identifier, and
processing-run identifier. The payload contains no PDF bytes, object-storage key, filename, raw
text, ORM object, URL, credential, retry policy, or client-supplied authorization claim.

Celery serialization accepts JSON only. The adapter validates the payload before publish and again
before execution. The worker reconstructs trusted execution context and verifies that the run and
workspace agree in PostgreSQL. A workspace identifier in a queue message scopes lookup; possession
of the message does not itself authorize access.

### Canonical execution lease

M0 extends `ProcessingRun` with the minimum execution metadata:

| Field | Meaning |
| --- | --- |
| `attempt_count` | Number of successfully claimed execution attempts |
| `lease_id` | Random identifier owned by the current attempt |
| `lease_expires_at` | UTC time after which another delivery may reclaim the run |

The maximum attempt count and lease duration are versioned processing configuration. The lease is
longer than the hard parser/task timeout plus bounded shutdown grace. The worker does not renew it
during the M0 bounded parse; a future longer workflow requires stage-specific leases and heartbeats.

A claim runs in one short transaction with a row lock:

1. reject an unknown, foreign, incompatible, or exhausted run without revealing foreign existence;
2. return a terminal no-op for an already `LEXICAL_READY` or `FAILED` run;
3. claim `CAPTURED`, increment `attempt_count`, set `PARSING`, and set a new lease;
4. refuse concurrent work while a `PARSING` lease is still active;
5. reclaim `PARSING` only after its lease expires and the attempt ceiling permits another attempt.

Parsing and object reads happen after the claim transaction closes. Completion and failure each use
a new short transaction and require the matching `lease_id`. A stale worker cannot commit output or
failure after another attempt has reclaimed the run. Terminal transitions clear the lease fields.
Derived blocks, chunks, and `LEXICAL_READY` are committed atomically as already established in
M0.0.

For a transient infrastructure failure, the attempt leaves a lease that is immediately expired and
Celery schedules a bounded retry with backoff and jitter. For a permanent parser, schema,
authorization, version, or limit failure, the matching attempt records the safe canonical `FAILED`
state and is acknowledged without retry. Reaching the attempt ceiling records a safe terminal
failure rather than requeueing indefinitely.

### Publish, acknowledge, and recovery

Document capture commits the immutable-original metadata and `ProcessingRun(CAPTURED)` before the
Celery adapter publishes. Publication is never part of the database transaction. A publication
failure does not roll back or hide the captured document; it leaves the run visibly `CAPTURED` and
records a safe structured operational event.

M0 provides a narrowly scoped developer command that finds eligible local `CAPTURED` runs through a
workspace-scoped application service and republishes their typed jobs. It cannot accept an arbitrary
SQL predicate or mutate terminal runs. M2 replaces this manual recovery gap with a transactional
outbox and reconciliation.

Processing tasks use late acknowledgement only after lease and idempotency tests pass. Successful
completion, permanent failure, and terminal no-op are acknowledged. Worker loss permits redelivery;
the canonical lease and attempt ceiling, not broker delivery count alone, prevent parallel execution
and infinite loops. Celery result storage is disabled because clients read PostgreSQL status.

## Consequences

The system can recover from a worker crash without holding a database transaction during parsing.
Duplicate tasks cannot create duplicate active outputs, and a stale worker cannot overwrite a newer
attempt. PostgreSQL explains the current processing state even if Redis is cleared.

M0 adds three nullable/managed execution fields and claim logic to `ProcessingRun`. A crashed worker
may delay recovery until the bounded lease expires. Clock behavior must therefore use aware UTC and
be tested deterministically. Operators still need the narrow republish command after a publish
failure until M2 implements automatic outbox delivery.

The lease is deliberately specific to the bounded M0 processing run. It does not replace the later
stage-level retry history, durable job intent, scheduling, reconciliation, or user-facing retry
workflow.

## Validation and reconsideration triggers

Before enabling late acknowledgement, tests must prove:

- two simultaneous deliveries produce one lease owner and one canonical output;
- active leases cannot be stolen and expired leases can be reclaimed;
- a stale lease owner cannot complete or fail a reclaimed run;
- worker loss after claim allows bounded redelivery and recovery;
- duplicate delivery after `LEXICAL_READY` is a terminal no-op;
- permanent failures are not retried and transient failures stop at the attempt ceiling;
- broker publication failure preserves `CAPTURED` and the narrow repair path republishes it;
- wrong-workspace and unknown job payloads have the same external outcome;
- task payloads and logs contain no original bytes, raw text, secrets, or internal exception details.

Reconsider the fixed lease duration if measured parse work approaches it. Do not add unbounded
renewal silently; longer or multi-stage work should move to the M2 stage/heartbeat model. Supersede
the M0 publish-recovery portion when the transactional outbox becomes canonical.

## References

- [Celery task acknowledgement and idempotency](https://docs.celeryq.dev/en/latest/userguide/tasks.html)
- [Celery worker-loss and acknowledgement configuration](https://docs.celeryq.dev/en/main/userguide/configuration.html)
