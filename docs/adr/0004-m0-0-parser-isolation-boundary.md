# ADR-0004 — M0.0 parser isolation boundary

**Status:** Accepted

**Date:** 2026-09-13

**Scope:** M0.0 PDF processing and the isolation gate before broader document use

## Context

M0.0 parses the frozen synthetic PDF in a development runner outside the FastAPI process. The
runner owns a fresh database session and applies page, extracted-text, memory, and wall-clock
limits. It still loads the application settings and constructs the parser in the same operating
system process that can reach PostgreSQL and local object storage. Resource bounds therefore do not
provide secret, filesystem, or network isolation from parser code.

The original M0.0 security wording required parsing without application secrets or unnecessary
network access. The implemented runner does not satisfy that stronger production boundary, and it
must not be described as if it does. M0.0 is a local product proof over one repository-owned,
synthetic fixture; it is not an approval to process arbitrary or private documents.

## Decision drivers

- describe the implemented trust boundary accurately;
- preserve the useful process, transaction, and resource separation already proven by M0.0;
- prevent local proof controls from being mistaken for hostile-file isolation;
- avoid adding a partial subprocess wrapper that still shares filesystem and network capabilities;
- require testable isolation before the accepted input scope grows;
- keep the processing application contract reusable by the later worker runtime.

## Options considered

1. **Treat the existing runner as fully isolated.** Rejected because a separate process and resource
   limits do not remove credentials, filesystem access, or network capability.
2. **Add a lightweight parser subprocess during M0.0.** Rejected because merely clearing selected
   environment variables would not enforce filesystem or network isolation and would create a false
   security claim.
3. **Build a production isolation runtime during M0.0.** Technically stronger, but disproportionate
   to the frozen synthetic proof and coupled to the worker and container choices scheduled for M0.
4. **Accept a narrow, explicit M0.0 exception and make isolation a mandatory M0 gate.** Selected
   because it keeps the current proof honest without allowing the exception to expand with usage.

## Decision

M0.0 retains the separate bounded development runner for the single frozen fixture. Its guarantees
are limited to:

- parsing outside the FastAPI request process;
- a new runner-owned database session and transaction boundary;
- configured page, extracted-text, memory, and wall-clock limits;
- immutable original storage and safe terminal processing states;
- local-only execution without private, sensitive, or personal archive data.

M0.0 does **not** claim operating-system isolation of parser code. The parser process can currently
access the runner's database configuration, local filesystem permissions, and available network
interfaces. Processing arbitrary external PDFs is outside the accepted M0.0 boundary even when the
UI can technically submit them.

M0 must establish and test a parser execution boundary before M1 accepts documents beyond the frozen
fixture. The boundary must provide:

- no application or provider credentials in the parser environment;
- only the bounded input and output paths required for one parse;
- denied unnecessary network access;
- a least-privilege runtime identity and restricted filesystem view;
- enforced CPU, memory, output-size, and wall-clock limits;
- safe termination and an inspectable canonical `ProcessingRun` failure state.

The exact Linux sandbox, subprocess, or dedicated-container mechanism is selected during M0 with the
worker runtime. It must be demonstrated by automated negative tests; documentation or environment
variable clearing alone is not sufficient evidence.

## Consequences

The M0.0 evidence path remains useful and its current protections are stated precisely. The proof can
be completed without pretending that a local runner is a hostile-file sandbox. The frozen fixture,
processing contracts, state transitions, and deterministic tests remain reusable.

The accepted input scope is narrower than the upload form's technical PDF capability. Developers
must not use M0.0 for private or untrusted documents. M0 gains a security-critical implementation
and verification task before M1; failure to satisfy it blocks broader document ingestion.

## Validation and reconsideration triggers

Keep the M0.0 runner limit and failure-state tests. During M0, add tests proving that parser code
cannot read injected secret markers, access unrelated filesystem paths, or establish blocked network
connections, and that resource-limit failures leave the original and canonical run state intact.

Reconsider this decision if a small, portable isolation mechanism can satisfy those tests without
the M0 worker runtime, or if the M0.0 input scope is proposed to expand. Any such expansion requires
the isolation gate first rather than silently broadening this exception.
