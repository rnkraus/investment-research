# ADR-0001 — Modular monolith with asynchronous workers

**Status:** Accepted

**Date:** 2026-09-13

**Scope:** V1 application ownership and M0 runtime process boundaries

## Context

The archive already implements its M0.0 evidence path as one Python application package with
explicit Identity, Documents, Processing, Search, Research, and Evidence modules. FastAPI invokes
typed application services, while a separate development runner invokes the same processing
service outside the request process.

M0 replaces that runner with Redis and Celery and adds a restricted parser boundary. This creates
multiple operating-system processes but does not by itself require independently owned business
services. Splitting the domain prematurely would add network contracts, distributed failure modes,
deployment coordination, and duplicated authorization work before scale or team ownership requires
them.

## Decision drivers

- keep CPU-heavy and retryable work outside FastAPI request execution;
- preserve the tested application services, ports, workspace context, and transaction boundaries;
- keep module ownership visible and enforceable inside one codebase;
- avoid distributed business transactions and premature service APIs;
- allow API and worker processes to scale or fail independently;
- keep a future extraction path when measured operational or ownership pressure justifies it.

## Options considered

1. **Run all work inside FastAPI.** Operationally simple, but parsing and later indexing would block
   or compete with request handling and would not provide durable asynchronous delivery.
2. **Use one modular application package with separate API and worker processes.** Preserves one
   domain model and typed application boundary while separating request and background execution.
3. **Split Processing, Search, and Research into microservices now.** Provides independent network
   services but introduces distributed state and operations without a measured need.
4. **Adopt a workflow framework as the central application architecture.** Rejected because M0 has
   one bounded processing workflow and the application-owned services already express its logic.

## Decision

V1 uses a modular monolith with separate runtime processes:

- FastAPI owns synchronous HTTP and streaming entry points;
- Celery workers own asynchronous delivery entry points;
- a restricted parser process is an infrastructure security boundary, not a business microservice;
- all three entry points are built from this repository and depend on application-owned contracts;
- business modules retain their domain, application, contract, port, and persistence ownership;
- entry points and infrastructure adapters may compose modules but may not contain domain policy;
- one module calls another through a typed application service or declared port, never through the
  other module's persistence implementation;
- Celery task definitions remain thin adapters. Queue payloads are versioned application data and
  do not expose Celery objects to domain or application code;
- PostgreSQL transactions remain local to one application operation. No distributed transaction is
  introduced across PostgreSQL, Redis, MinIO, or future providers;
- API, worker, and parser images may have different commands, settings, credentials, networks, and
  resource limits while sharing reviewed application code where their responsibilities overlap.

The restricted parser exposes only the bounded parse contract needed by the worker. It owns no
canonical data, has no independent product API, and does not create a new domain boundary.

## Consequences

The API can remain responsive while background work retries independently. Existing M0.0 domain
logic and tests remain useful because Celery changes delivery rather than business ownership. Module
calls stay local and type-checked, and most failures remain ordinary application or transaction
failures instead of cross-service consistency problems.

The shared codebase requires architecture tests and review discipline to prevent accidental module
coupling. API and worker releases normally move together. A failure or resource leak inside one
worker can affect other work assigned to that worker process, so tasks and queues still need bounded
concurrency, resource limits, idempotency, and observable terminal states.

The parser boundary adds one internal runtime hop. That cost is accepted for security isolation and
does not authorize other modules to become network services.

## Validation and reconsideration triggers

Maintain architecture tests for import direction, thin entry points, workspace propagation, and the
absence of cross-module persistence imports. Run the same processing application tests through both
direct test adapters and Celery delivery. Verify API and worker lifecycle and failure behavior
independently.

Reconsider extraction of a module only when measurements show a sustained need such as:

- materially different scaling or availability requirements that process separation cannot meet;
- an independently owned team and release cadence;
- a security boundary that cannot be enforced with the current process/container model;
- technology requirements incompatible with the shared runtime;
- repeated deployment contention attributable to one module.

Any extraction must preserve workspace authorization, canonical ownership, idempotency, evidence
validation, and typed contracts. A larger codebase alone is not sufficient justification.
