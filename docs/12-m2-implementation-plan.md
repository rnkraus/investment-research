# Investment Research Archive — M2 Implementation Plan

**Status:** Draft for review — scheduled after M1.5; no M2 behavior is implemented by this document  
**Date:** 2026-10-01  
**Milestone:** M2 — Reliable ingestion and lifecycle

**Deletion-policy update (2026-10-12):** The owner approved the local permanent-deletion path in
[ADR-0019](./adr/0019-permanent-local-document-deletion.md), including complete dependent-answer
removal and detached cost retention. Its no-restoration policy supersedes every older restore or
30-day recovery proposal in this draft. Schema work is in progress; this does not accept the full
M2 lifecycle gate. The remaining non-deletion decisions below still require their normal review.

**Sources of truth:** [Delivery plan](./05-delivery-plan.md),
[M1-to-M2 handoff](./10-m1-implementation-plan.md#81-m1-to-m2-handoff),
[M1.5 RAG quality plan](./13-m1-5-rag-quality-plan.md),
[domain model](./03-domain-model.md), [security and operations](./04-security-and-operations.md),
and accepted [ADRs](./adr/README.md). M1's frozen contracts remain in
[11-m1-contracts.md](./11-m1-contracts.md); this plan extends them rather than rewriting them.

## 1. Outcome and boundaries

M1 already captures one PDF or pasted-text original, processes it asynchronously, searches the
current eligible content, and produces a validated answer with an exact source location. The local
research form can ask about saved eligible documents without another upload. M1.5 may improve the
retrieval and citation-selection policy before M2 begins, but does not change M2's lifecycle
responsibilities. M2 makes the path reliable when imports, workers, providers, versions, and
deletion do not proceed perfectly.

The M2 exit gate is the one in the delivery plan: replaying any job or outbox event produces the
same final canonical state, while deleted or inactive content cannot become evidence even if stale
vectors remain. The existing PDF, pasted-text, citation, insufficient-answer, workspace-isolation,
provider-failure, recovery, and cleanup gates must keep passing.

M2 does **not** add archive browsing, companies, watchlists, metadata filters, RRF, reranking,
conversation memory, authentication, public hosting, or agent workflows. RRF and reranking belong
to M1.5 experiments; the remaining features retain their M3–M9 owners. M2 must preserve whichever
retrieval policy M1.5 accepts. The fixed local owner and loopback-only runtime remain in force;
only approved public or non-sensitive material may reach external AI providers.

## 2. Existing contracts and the M2 delta

| Boundary    | M1 today                                                                                                                      | M2 extension                                                                                                                               |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Capture     | A new `Document`, version 1, immutable original, and `ProcessingRun` are recorded together; queue publication follows commit. | Multiple PDFs, later immutable versions, exact-duplicate provenance, and a durable dispatch event in the capture transaction.              |
| Processing  | One leased `ProcessingRun` resumes parsing or indexing after redelivery.                                                      | A durable `Job` view and independently retryable `ProcessingStage` attempts expose safe failure and repair history.                        |
| Dense index | Deterministic vector IDs and bounded visibility checks precede `SEARCHABLE`.                                                  | Durable index intent and receipts explain partial writes; reconciliation repairs missing, stale, and orphaned vectors.                     |
| Activation  | One current document version and active chunk set are used for new retrieval.                                                 | A candidate version or reprocessing run stays inactive until its complete derived output is ready; one PostgreSQL transaction promotes it. |
| Deletion    | No user-facing lifecycle operation.                                                                                           | Immediate irreversible canonical exclusion, exact scoped purge, dependent-answer removal, and preserved content-free costs under ADR-0019. |

The intended flow is:

```mermaid
flowchart LR
    A[PDF or text capture] --> B[Immutable original]
    B --> C[PostgreSQL: version, run, job, outbox event]
    C --> D[Outbox dispatcher]
    D --> E[Celery delivery]
    E --> F[Retryable processing stages]
    F --> G[Canonical chunks and index intent]
    G --> H[Pinecone write and receipt]
    H --> I[Atomic canonical activation]
    I --> J[Current retrieval and validated evidence]
    C -. canonical intent .-> K[Reconciliation]
    K -->|repair or cleanup| H
```

PostgreSQL and immutable object storage remain canonical. Redis transports work; Pinecone remains
rebuildable. A queue acknowledgement, vector match, or receipt is never authorization proof.
External calls and parsing occur outside database transactions. Workspace, current-version,
active-output, deletion, and temporal eligibility are rechecked before a result becomes evidence.

## 3. Decisions to freeze before implementation

These are recommended M2 contract choices, not claims about implemented behavior. Review them in
the first work block; record an ADR only if a final choice changes an accepted cross-cutting rule.

1. **Version activation.** Keep `Document.current_version_id` as the active version for new
   research. Capturing version 2 records a pending immutable version without moving that pointer.
   Promote it only after its required processing and index visibility pass. A failed candidate
   leaves version 1 searchable. New documents may point at their first version before it becomes
   searchable, as M1 already does; processing state still prevents premature retrieval.
2. **Duplicate and request replay semantics.** Scope exact content-hash detection to one workspace
   and media type. A separate intentional import retains its own document/version and records a
   `DuplicateRelation`; it is never silently merged. Repeating the _same request idempotency key_
   returns the same result or a safe conflict for different input, rather than creating another
   original. Do not deduplicate physical originals across workspaces.
3. **Batch behavior.** Bound both per-file and total request size/count. Treat each accepted PDF as
   its own capture transaction and return a result for each item; one rejected file must not hide
   successfully captured siblings. Freeze the API's partial-success shape and retry behavior before
   changing the frontend.
4. **Historical citation access.** Old `EvidenceSpan` identities, source offsets, and immutable
   versions must survive reprocessing and version promotion. Current M1 source opening rejects an
   inactive version. Decide explicitly whether an authorized historical-answer view may open a
   retained inactive version; keep it separate from _new-answer_ eligibility. Do not loosen the
   existing endpoint silently. Pending-deletion and purged content must never be exposed through it.
5. **Purge retention matrix.** ADR-0019 freezes source-bound answer removal, exact-target cleanup,
   content-free receipts/accounting, and backup-expiry boundaries for the local path. There is no
   application restoration period. M2 must extend and verify delivery/reconciliation without
   weakening those decisions; a public or multi-user retention policy needs separate review.
6. **Periodic repair trigger.** Prefer one separately run Celery Beat scheduler using the existing
   broker and worker image, not an embedded worker `-B` or a new backing service. PostgreSQL remains
   the work authority, so duplicate or missed ticks are harmless; a bounded operator command can
   drain due work if the scheduler is unavailable. Validate the Compose and CI topology before
   accepting this process addition.

No new production dependency is approved by this plan. If implementation needs one, follow the
repository's version, security, release-age, compatibility, and explicit-approval rules first.

## 4. Planned repository delta

These are target ownership locations, not files already present. Names and physical tables are
confirmed against the frozen Step 0 contracts before a migration is written.

| Owner                                                                                 | Intended change                                                                                                                                                                              |
| ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `modules/documents/`               | New-version capture, duplicate provenance, lifecycle transitions, and the existing separate deletion contracts/services/ports. Preserve the original-storage port and ADR-0019 policy. |
| `modules/processing/{domain,contracts,ports,application,persistence}.py`              | Stage state, attempt history, bounded retry/lease ownership, and existing `ProcessingRun` compatibility. Keep parser and provider work outside transactions.                                 |
| `modules/delivery/` (new, if Step 0 confirms ownership)                               | Typed `Job` and `OutboxEvent` contracts, scoped repositories, due-event claiming, dispatch, and safe retry policy. Application modules use declared ports, not another module's persistence. |
| `modules/search/{ports,indexing,persistence}.py` and `modules/processing/indexing.py` | Durable index intent, receipts, desired-versus-observed reconciliation, and active-output promotion. Pinecone SDK behavior stays behind `infrastructure/search/`.                            |
| `infrastructure/database/{unit_of_work,metadata}.py`                                  | Compose capture, stage, outbox, receipt, and promotion writes in short PostgreSQL transactions. Repositories never commit independently.                                                     |
| `infrastructure/queue/`, `bootstrap.py`, `compose.yaml`                               | Thin event/task adapters, one repair trigger, process wiring, and health checks without another business service.                                                                            |
| `api/routes/{documents,processing}.py`                                                | Version/batch capture, status/history, retry, reprocess, and permanent-deletion contracts. Preserve the separately approved local catalog; no broad company discovery API. |
| `backend/migrations/versions/`                                                        | Sequential Alembic revisions after M1 head `0008`; each work block includes constraints, backfill/compatibility, indexes, and an upgrade test. No `create_all()` in application code.        |
| `frontend/src/features/archive/`                                                      | Minimal batch/version, stage/error/retry, reprocess, and permanent-deletion controls in their owning steps. Preserve the approved local catalog; M3 owns broader archive discovery. |
| `backend/tests/`, `frontend/`, `.github/workflows/ci.yml`, runtime verifiers          | Offline correctness and PostgreSQL/Compose failure gates; live providers remain bounded, optional, and public-synthetic only.                                                                |

At minimum, Step 0 reviews the need for `jobs`, `outbox_events`, `processing_stages`, stage-attempt
history, `duplicate_relations`, request-idempotency records, index intents/receipts,
`reconciliation_runs`, and document lifecycle fields. A table is added only when it owns durable
state that the existing models cannot represent without ambiguous authority.

## 5. Implementation sequence

Each numbered step is one bounded implementation step. Within it, complete backend behavior and
its focused tests first, then required CI checks, then real frontend behavior, and finally its full
gate. If there is no frontend change, run the existing frontend checks without adding artificial
UI files. Pause between coherent work blocks for review as required by `AGENTS.md`.

### Step 0 — Freeze contracts and compatibility (documentation gate)

- Decide the six contracts in Section 3, including public API schemas and safe error categories.
- Freeze stage names, retry classes/ceilings, event schema and idempotency keys, receipt identity,
  lifecycle timestamps, and the purge retention/FK matrix. Derive limits from existing settings;
  do not invent unverified configuration names or numeric values in documentation.
- Specify migration/backfill behavior for M1 runs and answers. Do not invent historical stage
  attempts that never occurred; mark legacy state honestly.
- Add synthetic fixtures for two versions, exact duplicates, partial index visibility, stale
  vectors, worker loss, interrupted permanent deletion, and a known historical citation.

**Gate:** Contracts, migration ownership, security implications, and the first implementation work
block are reviewable without any M2 runtime code. Resolve conflicts with accepted ADRs before Step 1.

### Step 1 — Durable outbox delivery for the existing M1 job

**Backend:** Persist one versioned, workspace-scoped `OutboxEvent` in the same transaction as a new
`ProcessingRun(CAPTURED)`. Keep the object-first immutable capture and narrow compensation rule.
Claim due events in bounded PostgreSQL batches with row locking/`SKIP LOCKED`, a short claim lease,
attempt ceiling, safe error class, and deterministic event identity. Publish the existing small
`processing-job.v1` payload outside the claim transaction; mark delivery in another transaction.
A crash between publish and receipt can publish twice, so the current run lease and idempotency
checks remain authoritative. Publication is not proof of worker execution: periodic repair also
re-examines canonical `CAPTURED` runs and expired leases after a grace period, even if an event was
marked published before Redis lost its message. Add one periodic drain trigger plus a narrow manual
repair command.

**CI:** Prove database rollback leaves no event, broker outage leaves due canonical intent, duplicate
dispatch has one effect, competing dispatchers claim disjoint events, expired claims recover, and
Redis loss both before and after a publication receipt eventually processes the captured run.
Verify the scheduler boundary without a live paid provider.

**Frontend:** No new UI. Existing capture and status remain compatible.

**Gate:** Capture succeeds with Redis down; after Redis returns, the run advances without manual
database repair and without duplicate canonical output. The M1 runtime gate still passes.

### Step 2 — Processing stages, attempts, and safe retry controls

**Backend:** Add `ProcessingStage` state and attempt history for the required parse/lexical and
embed/index work. Each stage has a stable run-scoped identity, versioned input/output references,
bounded attempt policy, lease owner and expiry, and a safe terminal error category. Resume a
completed stage without repeating it; reclaim only expired attempts; prevent stale owners from
committing. Expose scoped stage status and a retry action for eligible failed work. Keep the M1
`ProcessingRun` state as the aggregate readiness view, not a second conflicting authority.

**CI:** Cover concurrent/duplicate delivery, crash after claim, transient versus permanent error,
attempt exhaustion, parser limits, partial output rollback, wrong-workspace/unknown equivalence,
and upgrade of existing M1 rows without fabricated attempt history.

**Frontend:** Show stage-level progress, safe failure text, and a retry control only when the API
reports that the operation is eligible. Do not expose parser internals or credentials.

**Gate:** An eligible failed stage retries without recomputing earlier completed stages; permanent
failures and exhausted attempts remain visible and cannot loop forever.

### Step 3 — Multiple imports, immutable versions, and exact duplicates

**Backend:** Extend PDF and pasted-text capture through the existing validation and immutable
storage boundary. Add bounded multi-PDF capture, an explicit create-version operation guarded by
trusted workspace and expected current-version state, request-idempotency records, and scoped
exact-hash `DuplicateRelation` records. Keep each intentional import's provenance. A pending new
version cannot become a search or evidence source before promotion; the old active version remains
available if the new one fails. Return per-item and per-version processing identifiers.

**CI:** Test partial batch success, size/count limits, same-key replay and changed-payload conflict,
concurrent version numbers, identical bytes with distinct provenance, cross-workspace duplicates,
object/metadata partial failure, and the old-version availability window.

**Frontend:** Permit multiple PDFs and a later PDF/text version for a known document ID. Show each
item's capture/processing result; do not implement the M3 archive browser.

**Gate:** An import retry does not multiply canonical records; an intentional exact duplicate is
recorded rather than silently discarded; a failed pending version does not hide the old answerable
version.

### Step 4 — Index intent, receipts, and reconciliation

**Backend:** Create durable per-vector index intent from canonical chunks and configuration
versions. Upsert with existing deterministic vector IDs, record success/failure/visibility receipts,
and retry only outstanding intent. Reconciliation compares the desired active _and pending
candidate_ sets with bounded provider observation, repairs missing vectors, and schedules truly
stale/orphaned IDs for deletion. A candidate vector needed for a future atomic promotion is not an
orphan merely because it is not yet active.
Extend provider-neutral index ports for exact-ID deletion and bounded namespace listing only where
needed; do not authorize from Pinecone metadata. Record reconciliation runs and safe counts.

**CI:** Use deterministic adapters for partial upserts, delayed visibility, timeout, replay,
missing/stale/orphaned vectors, workspace isolation, bounded pagination, and cleanup idempotency.
An optional public-synthetic live check validates Pinecone-specific listing/deletion behavior.

**Frontend:** No new search UI; show existing processing state and safe repair status if the API
exposes it. M3 owns discovery screens.

**Gate:** Canonical active chunks and provider vectors converge after retries; stale provider hits
remain ineligible before cleanup completes. The current M1 citation gate still passes.

### Step 5 — Reprocessing and atomic promotion

**Backend:** Reprocess an immutable `DocumentVersion` with a new `ProcessingRun`, parser/chunk/
embedding/index configuration versions, and shadow derived output. Do not mutate old blocks,
chunks, vectors, or evidence spans. After required stages and vector visibility succeed, promote
the new chunk set/run and, for a pending content version, `Document.current_version_id` in one
short PostgreSQL transaction with an expected-state check. Schedule old-vector cleanup through
durable intent; a failed candidate leaves the old active set unchanged.

**CI:** Race promotion with research, delete, another promotion, provider failure, and retry. Prove
there is no mixed old/new answer context, no activation gap, and historical citation IDs/offsets
remain unchanged. Exercise the explicit historical-view policy from Step 0.

**Frontend:** Add a reprocess action and show pending versus active outcome for the document being
handled. No archive-wide reprocessing screen.

**Gate:** Replaying promotion is a no-op; only one compatible active set is used for new research,
and a failed candidate cannot displace it.

### Step 6 — Permanent local deletion and bounded purge

**Backend:** Follow ADR-0019: `ACTIVE → PURGE_PENDING`, then physical content removal and a
`COMPLETED` deletion receipt. Commit canonical exclusion and scoped intent together; perform exact
object/vector cleanup outside transactions, then remove source rows and complete affected research
history in a short transaction. Preserve detached content-free provider accounting and the minimal
receipt. No restoration, recovery deadline, broad filesystem, bucket, namespace, or database delete.

**CI:** Test research and citation denial immediately after canonical exclusion, stale-vector rejection,
no restoration path, repeated and interrupted purge, object/provider failure,
cross-workspace non-disclosure, and retention/FK behavior. Document the required 35-day backup
expiry boundary; M5 owns the later full backup-and-restore exercise. Use isolated synthetic
resources only; never purge the user's normal local archive during verification.

**Frontend:** The separately approved catalog follow-up places confirmed Delete selected above the
document list and supports explicit bounded multi-selection. Distinguish pending cleanup from
completed deletion; do not offer restore or claim cross-store completion before it is verified.

**Gate:** Deleted content cannot enter a new answer even with stale vectors; no restore operation
exists; repeated purge has the same content-free terminal result and does not reset cost accounting.

**Local follow-up status:** Admission/purge services, exact-key/vector adapters, detached accounting,
migration `0011_document_purge_guards`, and bounded Celery purge delivery are implemented. The
isolated queue-recovery gate verifies PostgreSQL and unversioned SeaweedFS cleanup. Endpoint,
website activation, and live Pinecone deletion verification remain pending. This does not complete
M2's durable outbox or periodic reconciliation work.

## 6. Final M2 acceptance and review

Run a clean-database migration to Alembic head, an upgrade from M1 data, drift checks, backend
format/lint/type/unit/PostgreSQL tests, frontend format/lint/type/contracts/tests/build, and the
isolated Compose journey. Add a synthetic failure matrix for Redis outage, worker crash, duplicate
message/event, partial Pinecone write, provider timeout, stale vector, version promotion, delete,
permanent purge, and detached accounting. Preserve the M1 PDF/text, answerable/unanswerable, exact-citation, and
cross-workspace cases. No normal CI gate calls paid providers.

Review migrations, repository boundaries, queue payloads, object keys, logs, and traces for
workspace leakage and raw content. Verify that all user-facing state transitions have safe English
messages. Update compact specifications and affected module READMEs as each step lands; record an
ADR only for a genuinely new or changed cross-cutting decision. M2 is not accepted merely because
all happy-path requests succeed.

The most important acceptance sequence is:

```text
Capture → lose Redis → recover delivery → retry a failed stage → index partially
→ reconcile → activate once → answer with exact evidence → request permanent deletion
→ reject a stale vector → retry interrupted cleanup → verify purge and preserved costs
```

Each arrow must be observable through canonical state or a safe operational record. Destructive
purge cases run only in isolated disposable test projects.

## 7. Stop conditions and references

Stop before implementation when an unresolved choice would alter source access, retention,
workspace authorization, a provider credential, or the public API contract. Stop before live
provider work if its fixture, limits, cleanup, and cost approval are not explicit. Do not use a
successful Pinecone write, Redis delivery, or model response as proof that canonical state changed.

The scheduling and claim approach is informed by the official
[Celery periodic-task guidance](https://docs.celeryq.dev/en/main/userguide/periodic-tasks.html)
and PostgreSQL's [queue-like `SKIP LOCKED` behavior](https://www.postgresql.org/docs/17/sql-select.html).
Pinecone documents [bounded serverless vector-ID listing](https://docs.pinecone.io/reference/api/2025-04/data-plane/list);
the exact SDK and Starter-plan behavior still require Step 4 verification.
