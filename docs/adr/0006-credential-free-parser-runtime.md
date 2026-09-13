# ADR-0006 — Credential-free parser runtime boundary

**Status:** Proposed

**Date:** 2026-09-14

**Scope:** M0 isolation topology for untrusted PDF parsing

## Context

ADR-0004 permits M0.0 to parse only the frozen synthetic fixture without claiming operating-system
isolation. M0 must remove that exception before the accepted input scope can expand. The parser must
handle untrusted bytes without receiving database, broker, object-storage, application, or provider
credentials and without gaining unnecessary filesystem or network access.

The API and Celery worker still need access to canonical services. The worker must read an immutable
original, invoke parsing, validate the parser response, and persist derived records. Putting parser
code directly in that worker would therefore give hostile document processing the worker's broader
permissions.

A Docker Desktop feasibility probe on 2026-09-14 demonstrated that the intended container controls
can provide a worker-to-parser channel while denying the parser access to a separate backend
network and the external network. The probe also confirmed a non-root identity, no effective Linux
capabilities, `no-new-privileges`, a read-only root filesystem, a bounded writable `/tmp`, and the
absence of synthetic worker-secret markers in the parser environment.

The probe used `postgres:17-alpine` only as an already-available Alpine shell image. It proves the
local Docker networking and runtime-control mechanics, not the security of the final parser image,
parser application, protocol, or Linux CI topology.

## Decision drivers

- isolate untrusted parser code from canonical stores and application credentials;
- retain the existing application-owned `DocumentParser` contract;
- keep the worker responsible for authorization, storage access, validation, and persistence;
- permit only the communication required for one bounded parse;
- make the boundary testable on Docker Desktop and Linux CI;
- avoid presenting environment-variable filtering alone as a security boundary;
- keep the parser an infrastructure boundary rather than a separately owned business service.

## Options considered

1. **Parse inside the Celery worker.** Rejected because parser code would inherit database,
   object-storage, broker, and application access.
2. **Start a child process inside the worker container with a filtered environment.** Rejected
   because it would normally retain the worker container's network and filesystem reach.
3. **Run a dedicated parser container on the backend network.** Rejected because network reach to
   canonical services would remain available even if credentials were omitted.
4. **Run a dedicated parser container on a separate internal network shared only with the worker.**
   Proposed because it creates a narrow, testable channel while keeping the parser away from the
   backend network and external routes.
5. **Adopt a stronger VM or sandbox runtime immediately.** Deferred until measurements or threat
   review show that container isolation is insufficient for the approved local M0 scope.

## Proposed decision

M0 will use a dedicated parser runtime built from the repository's backend image and connected only
to an internal parser network. The Celery worker is the sole application component connected to
both the backend network and parser network.

The data flow is:

```text
MinIO -> worker -> bounded PDF request -> parser -> typed bounded result -> worker -> PostgreSQL
```

The parser never receives an object key or credentials and never reads canonical stores directly.
The worker opens the authorized immutable original, enforces the request-size limit, sends only the
bytes and non-secret versioned parser limits required for one operation, validates the complete
response as untrusted input, and then persists it through the existing processing application
service.

The worker-to-parser protocol must be private, bounded, and application-owned. It is transport for
the existing `DocumentParser` port, not a public product API. The final transport and message schema
must define strict content type, request and response size, parser/configuration version, timeout,
ordering, page and text bounds, and safe error categories before this ADR can be accepted.

The parser runtime must:

- run as a fixed non-root user;
- use a read-only root filesystem and a size-bounded, `noexec`, `nosuid`, `nodev` temporary mount;
- drop all Linux capabilities and set `no-new-privileges`;
- apply CPU, memory, process-count, request-size, response-size, page-count, extracted-text, and
  wall-clock limits;
- receive no database, Redis, MinIO, AI-provider, tracing-provider, or application credentials;
- have no backend-network membership, public host port, external route, Docker socket, or broad
  host mount;
- expose only the narrow worker-to-parser listener on the internal parser network;
- return safe typed failures without document content or parser internals.

The API does not call the parser. The parser owns no domain model, database, queue, retries, or
canonical state. The worker owns retry classification and records the canonical `ProcessingRun`
outcome using the lease rules in ADR-0005.

## Current validation evidence

The Docker Desktop feasibility probe passed these local checks:

- the worker received the expected parser response over the isolated parser network;
- the parser ran as UID `65532`;
- the parser reported zero effective Linux capabilities and `NoNewPrivs: 1`;
- a root-filesystem write failed while a write inside the bounded `/tmp` succeeded;
- synthetic application-secret markers injected only into the worker were absent from the parser;
- connections from the parser to the separate backend network and external network failed;
- the temporary containers and networks were removed after the probe.

This is preliminary evidence only. Acceptance still requires the final parser image and application
protocol to pass equivalent automated negative tests on Docker Desktop and Linux CI.

## Consequences

Parser compromise has a substantially narrower reachable surface than worker compromise. Canonical
credentials and storage access stay in the worker, while parsing remains replaceable behind the
existing application port. The topology also makes forbidden paths observable through negative
tests rather than relying on documentation alone.

The worker becomes a deliberate bridge between two networks and must never proxy arbitrary parser
requests. A network-only protocol adds serialization, validation, timeout, and container-operation
work. Container isolation also shares the host kernel; it is not equivalent to a virtual-machine
security boundary. The approved data classification and local-only M0 scope remain in force until a
later threat review changes them.

## Acceptance and reconsideration triggers

Change this ADR to `Accepted` only after all of the following pass with the final runtime:

1. the parser image contains only the required runtime and parser dependencies and runs non-root;
2. Docker Desktop tests prove the required channel and all prohibited secret, filesystem, network,
   privilege, and resource paths;
3. equivalent Linux CI tests pass without privileged mode or Docker-socket access in the parser;
4. malformed, oversized, timed-out, and crashed parsing requests leave the immutable original
   intact and a safe, inspectable canonical processing state;
5. the implemented Compose topology and protocol match this record and ADR-0004's removal gate.

Reconsider a stronger isolation mechanism before private or sensitive documents, public uploads,
additional active document formats, or an increased parser threat model are approved. Reconsider
the network service transport if a narrower portable mechanism can pass the same negative tests.
