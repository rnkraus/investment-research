# ADR-0002 — Canonical and derived store ownership

**Status:** Accepted

**Date:** 2026-09-13

**Scope:** V1 data authority, recovery, and cross-store consistency

## Context

The archive uses several stores for different workloads. PostgreSQL holds structured business and
provenance records. S3-compatible object storage holds immutable originals and later durable binary
artifacts. Redis delivers background work and may cache data. Pinecone will provide dense retrieval,
and external tracing may provide operational inspection.

These systems cannot participate in one portable distributed transaction. Without explicit
ownership, a queue message, vector, cache entry, or trace could accidentally become the only copy of
business state. Recovery, deletion, workspace isolation, and historical evidence would then depend
on a store that is temporary, externally managed, or intentionally rebuildable.

M0 replaces local originals with MinIO and introduces Redis/Celery. The authority and failure rules
must therefore be fixed before those adapters are implemented.

## Decision drivers

- preserve immutable source evidence and reproducible business history;
- make Redis, indexes, caches, and traces safe to lose or rebuild;
- avoid distributed transactions across infrastructure products;
- define repair behavior for partial cross-store operations;
- keep workspace, lifecycle, version, and temporal eligibility canonical;
- support later backup, restore, deletion, and index reconciliation.

## Options considered

1. **Put all data in PostgreSQL.** Provides one transactional store but is a poor fit for immutable
   PDF objects, vector retrieval, queues, and external traces.
2. **Let each specialized store own its local result.** Convenient for adapters, but makes recovery
   and authorization ambiguous and can leave business state only in Redis, Pinecone, or tracing.
3. **Assign explicit canonical, rebuildable, transient, and observational roles.** Uses each store
   for its strengths while keeping authority and repair rules clear.
4. **Coordinate all writes with distributed transactions.** Rejected because the selected services
   do not share a suitable transaction protocol and the operational cost is disproportionate.

## Decision

V1 assigns these roles:

| Store | Role | Authoritative contents | Loss or disagreement rule |
| --- | --- | --- | --- |
| PostgreSQL | Canonical structured state | workspaces, documents, versions, processing state and outputs, retrieval/answer/evidence history, configuration versions, jobs, audit and usage records | Restore from tested backup; its workspace, lifecycle, version, and eligibility state wins |
| S3-compatible object storage | Canonical object state | immutable originals and explicitly designated durable processing artifacts | Restore with object version/backup policy; database metadata alone cannot recreate an original |
| Pinecone | Rebuildable derived index | active vectors and filter metadata derived from canonical chunks and versions | Discard stale/orphaned records and rebuild from canonical manifests and receipts |
| Redis | Transient delivery/cache state | queue messages, locks, rate/cache data with a canonical recovery path | May be recreated; loss must not erase document, processing, or answer intent |
| LangSmith or other external tracing | Observational copy | sampled, redacted execution traces | Never used as authorization, evidence, audit truth, or the only usage record |
| Process logs and metrics | Operational signals | redacted events and aggregates | Useful for diagnosis, never the only business or evidence record |

PostgreSQL records that describe derived processing output remain canonical history even when their
content can be regenerated. Reprocessing creates new versioned rows; it does not silently rewrite
the rows used by historical answers.

An original is complete only when both the immutable object and its committed PostgreSQL metadata
agree. Capture writes the object first with conditional immutable creation, then commits metadata.
If the database transaction fails, the application attempts narrowly scoped compensating deletion
of that uncommitted object. It never overwrites an existing object to repair disagreement.

Redis transports a processing identifier but does not own processing status. A broker publication
failure leaves a canonical `ProcessingRun` in `CAPTURED`; a bounded repair operation can republish
it. M2 adds transactional outbox delivery and automated reconciliation.

Future PostgreSQL-to-Pinecone synchronization uses durable intent, deterministic vector IDs,
receipts, and reconciliation. Pinecone metadata filters are defense in depth; PostgreSQL eligibility
is rechecked before generation and citation.

Every key, message, index partition, receipt, trace, and repair operation carries workspace scope.
Possession of an identifier, object key, or queue message is not authorization proof.

## Consequences

Redis, caches, Pinecone, and traces can be cleared without losing the archive's authoritative
meaning. Recovery and incident decisions have a defined direction, and application modules do not
need to understand provider-specific consistency behavior.

PostgreSQL and object storage form a two-part canonical evidence boundary, so capture and purge need
careful compensating and idempotent workflows. There is a short interval in which an uncommitted
object can exist before cleanup. Verification and later reconciliation must detect this condition.

Canonical storage costs more than treating provider output as disposable application state, and
backups must cover both PostgreSQL and object storage. The benefit is that answers, evidence,
authorization, and deletion do not depend on a queue, vector index, or tracing vendor.

## Validation and reconsideration triggers

Maintain tests for object/metadata partial failure, duplicate immutable creation, workspace scope,
Redis loss, duplicate task delivery, stale index candidates, and trace failure. Exercise database
restore and object recovery before the local vertical slice is accepted; exercise vector rebuild
after Pinecone is introduced.

Reconsider this decision if:

- a future artifact cannot be recreated and therefore needs canonical object-store classification;
- provider retention or consistency behavior conflicts with deletion requirements;
- recovery tests show that the selected split cannot meet the documented objectives;
- a new store is proposed without an explicit authority and rebuild rule.

A new store is not approved until its canonical, derived, transient, or observational role and its
workspace, backup, deletion, and reconciliation behavior are documented.
