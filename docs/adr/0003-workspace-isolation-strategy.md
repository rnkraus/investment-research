# ADR-0003 — Workspace isolation strategy

**Status:** Accepted

**Date:** 2026-09-09

**Scope:** M0.0 application and persistence boundaries, with production reconsideration triggers

## Context

The archive must prevent records and original files from crossing workspace boundaries. UUIDs are identifiers, not authorization proof, and a client-supplied `workspace_id` cannot establish access. The same rule must hold across HTTP requests, later worker executions, PostgreSQL relationships, repository reads, and object storage.

M0.0 uses a server-owned fixed local identity, but its application contracts and data model are intended to evolve into authenticated multi-workspace operation. The first schema, scoped repositories, and local object-storage adapter now make the isolation approach an implementation constraint.

## Decision drivers

- deny cross-workspace reads and writes at more than one layer;
- avoid disclosing whether a foreign identifier exists;
- use the same trusted scope for API and later worker operations;
- make incorrect cross-workspace relationships fail in PostgreSQL;
- keep M0.0 local and simple without presenting it as production authorization;
- preserve a clear path to database Row-Level Security if the production threat model requires it.

## Options considered

1. **Application filtering only.** Simple, but one omitted predicate can expose another workspace and the database can still accept invalid relationships.
2. **PostgreSQL Row-Level Security only.** Strong database enforcement, but it does not replace application authorization, object-key scoping, or safe non-disclosure. It also requires a reviewed production-role and connection-pooling design that M0.0 does not yet have.
3. **Trusted application context, scoped repositories, and relational constraints.** Provides explicit application authorization boundaries and database relationship integrity with the current architecture.
4. **Option 3 plus Row-Level Security.** Adds defense in depth, but is deferred until production identities, roles, pooling, migrations, and operational access are concrete enough to test correctly.

## Decision

M0.0 adopts option 3:

- The server resolves a trusted `WorkspaceContext`; clients cannot select or prove authorization by submitting `workspace_id`.
- Application services pass that context explicitly to repository and object-storage operations.
- Repository reads and writes include the trusted workspace scope. Unknown, foreign, and otherwise ineligible identifiers produce the same public absence result.
- Every product table except `workspaces` carries `workspace_id`. Composite foreign keys include the workspace wherever they enforce relationships between workspace-owned records.
- Server-generated object keys begin with `workspaces/{workspace_id}/`. Storage adapters validate the trusted context and refuse path traversal, symbolic links, overwrite, and foreign prefixes.
- Later queue messages carry the workspace identifier as execution context, but possession of a message or identifier alone is not authorization.
- PostgreSQL Row-Level Security is not part of M0.0 and must not be claimed as implemented.

## Consequences

The application and PostgreSQL jointly prevent common cross-workspace mistakes, and negative isolation tests can exercise the same contracts used by normal code. Non-disclosing reads reduce identifier-probing signals. The design also works with the local filesystem adapter and later S3-compatible adapters.

The cost is repeated workspace predicates, compound keys, and explicit context plumbing. Code review and tests must continue to detect missing scope. Database constraints protect relationships but do not automatically filter every query, and the local development database role is not a production authorization model.

## Validation and reconsideration triggers

Maintain unit and PostgreSQL integration tests for same-workspace access, cross-workspace non-disclosure, compound relationship constraints, transactions, and storage-path isolation. Worker and API tests must use the same application-owned context boundary when those entry points are implemented.

Reconsider option 4 before any environment hosts mutually untrusted workspaces, and no later than the M5 security-hardening gate. Adopt Row-Level Security only with tests covering runtime roles, connection-pool context reset, migrations, background workers, support access, backup and restore, and authorized purge behavior.

Supersede this ADR if workspace ownership, authorization boundaries, or the canonical storage model changes materially.
