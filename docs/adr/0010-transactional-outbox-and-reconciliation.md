# ADR-0010 — Transactional outbox and bounded reconciliation

**Status:** Accepted for M2 implementation — owner-approved contracts and bounds on 2026-10-14;
runtime implementation and activation tests remain outstanding.

**Scope:** M2 durable delivery, repair, and derived-index convergence. For M2 this supersedes
only ADR-0005's manual post-commit publication recovery gap, not its workspace, ID-only message,
at-least-once delivery, execution fencing, or bounded-attempt rules.

## Context and decision drivers

Capture currently commits a `ProcessingRun` before publishing to Redis. A failed publication can
leave durable captured content without a queued task. A successful publication receipt cannot
prove execution: Redis can lose a message later. Pinecone writes and canonical state likewise
cannot share one database transaction. The existing catalog and permanent-purge workflow must be
preserved without creating parallel job or deletion authorities.

## Options considered

1. Keep manual republishing only: preserves the known delivery gap and requires operator action
   for ordinary outages.
2. Depend on broker persistence and retries: cannot recover a message lost after publication and
   makes transient Redis state authoritative.
3. Hold database transactions around broker/provider calls: retains connections and locks during
   slow external work and still does not create a cross-store atomic commit.
4. Commit canonical outbox intent, publish outside transactions, and reconcile against PostgreSQL:
   selected. It adds bounded scheduling/repair work but keeps authority explicit.

## Decision

- Store workspace-scoped, versioned outbox intent with its business transaction. Reuse the
  existing `ProcessingRun` as the Step 1 business job and the existing typed broker payload.
- Claim due work with short, ordered row locks and `SKIP LOCKED`; publish outside the claim
  transaction and update receipts only with the matching claim fence. Duplicate publication is
  expected and must have one canonical effect.
- Reconcile captured or expired work even after publication succeeds. Preserve delivery attempts
  and generations; do not reset execution/provider attempt allowances on each scan.
- Use one separate Celery Beat process and the existing dependency/image. It has broker-only
  credentials and a minimal entry point, not worker bootstrap. Workers perform bounded scoped
  repair; a lost tick is replaced by a later canonical scan. Its writable schedule file is
  disposable, never business authority. No scheduler database or extra service dependency.
- Keep index desired state and write/visibility/absence receipts canonical. Retain unfinished
  candidate vectors until promotion/failure policy resolves them; reject stale hits before cleanup.
- Extend ADR-0019 purge delivery in Step 6, without changing its exclusion, exact-target cleanup,
  source-bound answer removal, or detached-accounting policy.
- Exhaustion is visible blocked work, not success or an unbounded automatic reset. Every repeated
  paid call still needs normal budget reservation. Manual repair uses the same scope and fences.

[M2 contracts](../15-m2-contracts.md) own the accepted schemas, numeric policy, migration behavior,
and synthetic failure matrix. Decision acceptance is not a claim of implemented behavior; preserve
the existing runtime until each owning step's activation gate passes.

## Consequences

Ordinary broker failure becomes recoverable from canonical intent, without holding transactions
during publication or trusting a broker acknowledgement as processing success. The system remains
at least once, not exactly once. It adds outbox rows, repair history, a scheduler process, and
operational monitoring. Long outages or repeated failures may exhaust automatic allowances and
require explicit recovery; they must not silently lose the captured document.

The scheduler is not permission to run cloud deployment, buy services, change credentials, or
make paid test calls. Existing local-only and public/non-sensitive-provider boundaries remain.

## Validation and reconsideration

Acceptance records the Step 0 contract review. Before runtime activation, prove rollback atomicity, competing
claims, stale-fence rejection, broker loss before and after a receipt, worker-loss recovery,
bounded exhaustion, duplicate ticks, wrong-workspace equivalence, and provider-independent CI.
Step 4 adds deterministic partial-index/stale-vector convergence; Step 6 adds interrupted-purge
reconciliation. Migration must preserve legacy state without inventing attempts or deleting intent.

Reconsider the single scheduler only after measured throughput or availability requires another
topology. Do not add an external workflow engine or distributed transaction without a new decision.

## References

- [Celery periodic tasks and single-scheduler guidance](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html)
- [PostgreSQL queue-like row locking](https://www.postgresql.org/docs/17/sql-select.html#SQL-FOR-UPDATE-SHARE)
- [Existing delivery ADR](./0005-at-least-once-processing-delivery.md)
- [Permanent local deletion](./0019-permanent-local-document-deletion.md)
