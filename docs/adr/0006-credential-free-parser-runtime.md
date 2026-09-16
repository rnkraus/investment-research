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
S3-compatible object storage -> worker -> bounded PDF request -> parser -> typed bounded result -> worker -> PostgreSQL
```

The parser never receives an object key or credentials and never reads canonical stores directly.
The worker opens the authorized immutable original, enforces the request-size limit, sends only the
bytes and non-secret versioned parser limits required for one operation, validates the complete
response as untrusted input, and then persists it through the existing processing application
service.

The worker-to-parser protocol is private, bounded, and application-owned. It is transport for the
existing `DocumentParser` port, not a public product API. M0 defines one
`POST /internal/v1/parse` operation. Its body is `application/pdf`; one size-bounded ASCII JSON
header carries `ParserRequestV1`, containing only the protocol version, parser version, parser
configuration version, and page and extracted-character limits. It contains no workspace,
document, storage, database, queue, provider, or credential value.

A successful `application/json` response is `ParserResponseV1`; a failure is
`ParserFailureV1` with one fixed safe category and no exception details or document text. The
worker bounds the original before sending it, uses a fixed endpoint and timeout, follows no
redirects, bounds response bytes before decoding, and validates schema, versions, page order, page
count, and extracted-character count. The parser independently validates request bytes and
metadata before parsing and output bounds before serialization. Response size is derived
deterministically from the requested page and character ceilings, including conservative JSON
escaping overhead, so independent free-form response limits cannot drift.

The parser runtime must:

- run as a fixed non-root user;
- use a read-only root filesystem and a size-bounded, `noexec`, `nosuid`, `nodev` temporary mount;
- drop all Linux capabilities and set `no-new-privileges`;
- apply CPU, memory, process-count, request-size, response-size, page-count, extracted-text, and
  wall-clock limits;
- receive no database, Redis, object-storage, AI-provider, tracing-provider, or application
  credentials;
- have no backend-network membership, public host port, external route, Docker socket, or broad
  host mount;
- expose only the narrow worker-to-parser listener on the internal parser network;
- return safe typed failures without document content or parser internals.

The API does not call the parser. The parser owns no domain model, database, queue, retries, or
canonical state. The worker owns retry classification and records the canonical `ProcessingRun`
outcome using the lease rules in ADR-0005.

## Current validation evidence

The preliminary shell-image feasibility probe passed on Docker Desktop on 2026-09-14. The final
repository-owned parser image, private protocol, Compose topology, and automated isolation gate then
passed on Docker Desktop on 2026-09-15. The final gate verified that:

- a worker-image client parsed the frozen PDF successfully through the isolated parser network;
- request-size and extracted-text violations produced bounded typed failures;
- the parser ran as UID `65532`, with zero effective Linux capabilities and `NoNewPrivs: 1`;
- the root filesystem was read-only while only the bounded `/tmp` mount remained writable;
- the declared memory, CPU, and process-count limits matched the inspected runtime;
- database, Redis, object-storage, and provider credential names and secret mounts were absent;
- no Docker socket, repository path, broad bind mount, or published host port was present;
- connections from the parser to Redis on the separate backend network and to an external address
  failed, while the sole worker-to-parser channel succeeded;
- temporary Compose resources, verification images, and read-only mode-`0444` secret files inside
  private mode-`0700` temporary directories were removed after the gate.

This satisfies the final-runtime Docker Desktop part of the acceptance trigger. The ADR remains
`Proposed` until the same boundary passes in Linux CI and timeout, crash, and malformed-response
tests prove the required canonical `ProcessingRun` outcomes.

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
