# Investment Research Archive — Security and Operations

**Status:** Draft v0.3
**Scope:** V1 local runtime and requirements for a later public deployment  
**Related:** [01-product-scope.md](./01-product-scope.md), [02-system-design.md](./02-system-design.md), [03-domain-model.md](./03-domain-model.md), [08-m0-0-implementation-plan.md](./08-m0-0-implementation-plan.md)

## 1. Goals and priorities

Security and operations protect the archive's evidence, privacy, and recoverability. The priority order is:

1. evidence integrity and workspace isolation;
2. protection of document content and credentials;
3. recoverability and deletion correctness;
4. observable, bounded AI behavior;
5. availability, latency, and cost.

No performance or convenience improvement may bypass authorization, evidence validation, or lifecycle rules.

The initial system runs locally with Docker Compose. Controls marked **public deployment** become release gates only when the application is reachable from the internet.

## 2. Data classification and external-provider policy

| Class | Examples | V1 handling |
|---|---|---|
| Public | Published annual reports, public articles, redistributable demo material | May use approved external AI, vector, and tracing providers |
| Personal non-sensitive | User notes without confidential or identifying content | Local storage; external processing only after explicit review |
| Private or sensitive | Paid research, confidential documents, personal data, credentials | Must not be sent to external providers in V1 |
| Secrets | API keys, tokens, signing keys, database credentials | Never stored in documents, prompts, logs, traces, or source control |

Before private or sensitive content is enabled, each provider must be reviewed for:

- transmitted fields and subprocessors;
- storage and training behavior;
- retention and deletion controls;
- region and cross-border processing;
- trace and support-access behavior;
- contractual and account settings.

The demo workspace contains only synthetic, openly licensed, or otherwise redistributable material.

### M1.5 reranking boundary

[ADR-0018](./adr/0018-pinecone-reranking-experiment.md) permits an opt-in public/synthetic-only
experiment sending a bounded question and canonically eligible passage text to Pinecone Inference.
This is a separate trust boundary from vector indexing: raw text remains forbidden in vector
metadata, normal logs, and traces. Neither the vector index's region nor the upstream model's
Apache-2.0 license establishes inference residency or retention guarantees. The decision records
the permitted endpoint, process-only credentials, validation, deadline/retry limits, and quota/cost
gates. No implementation, live run, paid-plan upgrade, private-data approval, or default adoption
is claimed by documenting this boundary.

### M0.0 proof constraints

The pre-foundation M0.0 proof is not a release environment. It must:

- use only the repository-owned, frozen synthetic fixture for accepted processing and research
  evidence;
- remain reachable only from the local development environment;
- contain no private, sensitive, or personal archive data;
- use an external AI provider only when the fixture is public and that provider use is explicitly approved;
- derive the fixed local identity and workspace context on the server rather than accepting client-supplied authorization scope;
- allowlist the supported PDF type, verify its content signature, enforce byte, page, memory, and processing-time limits, and use server-generated storage keys outside the web application root;
- keep original artifacts immutable, checksum-recorded, non-executable, and accessible only through an authorized API route;
- run parsing outside the API process in the separate bounded development runner with a new
  database session, while making no operating-system secret, filesystem, or network-isolation claim;
- validate typed generation output and every statement-to-evidence relationship before emitting answer content;
- exclude public hosting and make no availability, durability, retry, backup, or deletion guarantee.

The parser exception is governed by
[ADR-0004](./adr/0004-m0-0-parser-isolation-boundary.md). The current runner may technically parse
another PDF, but doing so is outside the accepted M0.0 security boundary. M0 must implement and test
the stronger parser boundary in section 5 before M1 accepts broader document input.

All M0.0-only data may be discarded before M0. No fixture or result is migrated into a private or demo workspace unless it independently satisfies the later provenance, licensing, and isolation requirements.

## 3. Trust boundaries and main threats

Treat the following as untrusted input:

- uploaded files, pasted text, filenames, and document metadata;
- instructions embedded inside documents;
- user prompts and client-supplied identifiers;
- model, parser, vector-search, and later tool output;
- third-party libraries, model versions, and provider configuration changes.

Primary threat scenarios:

| Threat | Required outcome |
|---|---|
| Cross-workspace access | No data, existence signal, object URL, search result, trace, or job detail crosses the authenticated workspace |
| Direct or indirect prompt injection | Document text remains evidence data and cannot change policy, reveal secrets, or enable tools |
| Malformed or hostile file | Processing stops within configured resource limits and reports a safe failure |
| Stale or poisoned retrieval result | Canonical authorization and active-version checks reject it before generation |
| Unsupported model statement | Server-side evidence validation rejects or downgrades it |
| Provider outage or rate limit | Requests fail clearly or retry within bounds; canonical state remains consistent |
| Duplicate delivery | Idempotency produces the same final state without duplicate active records |
| Sensitive-data leakage | Logs, traces, errors, and provider calls follow classification and redaction rules |
| Supply-chain change | Dependency, model, prompt, and configuration versions are reviewed and reproducible |

## 4. Identity and workspace isolation

- Authentication uses a standard OIDC provider; the application stores only the external subject identifier.
- Authorization is enforced in FastAPI and repository methods, never only in the web interface.
- Every request, job, object key, retrieval query, audit event, and usage record carries an explicit workspace context.
- Client-supplied `workspace_id` values are not trusted as authorization proof.
- The vector-index partition is defense in depth; canonical database validation remains mandatory.
- Demo access is read-only and isolated from the private owner workspace.
- Errors do not reveal whether an inaccessible object exists in another workspace.
- Cross-workspace tests cover APIs, repositories, object storage, queues, search, citations, traces, and caches.

[ADR-0003](./adr/0003-workspace-isolation-strategy.md) governs the current defense-in-depth boundary and records the production trigger for reconsidering PostgreSQL Row-Level Security.

M1 has one temporary exception: its fixed server-owned owner context may remain while the complete
RAG path is developed locally. The host may publish the web entry point only on loopback; backend
containers may bind inside private Compose networks but must not be directly exposed. The context
must remain behind `IdentityContextProvider`, and all workspace-scoped repositories and negative
isolation tests still apply. M5 must replace this adapter with OIDC identity, canonical memberships,
and server-side sessions before staging, shared-network, or public exposure. The exception must
never be enabled as a production fallback.

For a public deployment, use secure cookies or short-lived tokens, strict redirect and CORS rules, CSRF protection where applicable, security headers, and rate limits.

## 5. Safe document ingestion

Uploads must be processed as hostile data:

- allowlist supported file types and verify content signatures rather than trusting extensions;
- apply configurable byte-size, page-count, decompression, memory, CPU, and processing-time limits;
- normalize filenames and generate server-owned storage keys;
- calculate a checksum before processing;
- parse outside the API request process in a restricted worker;
- prevent parser access to application secrets and unnecessary network resources;
- keep originals immutable and record parser and configuration versions;
- expose safe failure reasons without returning parser internals or document content;
- add malware scanning before public uploads are enabled.

V1 accepts PDFs and pasted text only. URLs, archives, office files, scripts, and active document content remain disabled until separately designed and tested.

These ingestion controls are an M0 implementation gate, not a description of the narrower M0.0
runner. ADR-0004 records the temporary synthetic-fixture exception and its removal criteria. The
implementation and negative-test sequence is maintained in
[09-m0-implementation-plan.md](./09-m0-implementation-plan.md).

### Local object-storage boundary

[ADR-0007](./adr/0007-local-s3-provider.md) selects SeaweedFS only for the local Docker Compose
runtime and retains a provider-neutral S3 adapter for a later managed public service. The local
runtime must:

- pin the reviewed SeaweedFS image by version and digest rather than use a floating tag;
- keep the bucket private and avoid publishing object-storage ports beyond the documented local
  boundary;
- expose only the required S3 data-plane endpoint to API and worker processes;
- separate administrative bootstrap access from least-privilege application credentials;
- give object-storage credentials only to API and worker processes, never web or parser processes;
- restrict application access to the required bucket and operations, without ACL administration;
- preserve workspace-scoped server-generated keys and uniform inaccessible-object responses;
- test conditional create, checksum integrity, restart persistence, credential scope, endpoint
  reachability, and cross-workspace non-disclosure on Docker Desktop and Linux CI.

The local single-node volume is development persistence, not a backup or availability guarantee.
Before public deployment, select and validate a managed S3-compatible service with versioning,
encryption, lifecycle, backup, recovery, and provider-specific contract tests.

## 6. AI and RAG safety

- System instructions and authorization rules are supplied by the application, never derived from document content.
- Retrieved text is clearly delimited and treated as untrusted evidence.
- V1 exposes no web-search or write-capable agent tools to the model.
- Model output must match a typed schema before the application uses it.
- Every archive-derived answer statement passes evidence, workspace, active-version, and temporal validation.
- Conversation history can provide context but cannot become evidence.
- Search and context sizes, output tokens, retries, and provider timeouts are bounded.
- Missing evidence produces an insufficiency result rather than model-knowledge completion.
- Prompt, model, embedding, chunking, retrieval, and reranking versions are recorded.
- Injection fixtures include fake system messages, requests for secrets, fabricated citations, and instructions to ignore archive rules.

Later LangGraph workflows require separate approval criteria, tool allowlists, step/time/token/cost budgets, checkpoints, and explicit stop conditions.

## 7. Secrets, privacy, logs, and audit

### Local development

- Secrets are loaded from ignored environment files or a local secret mechanism.
- Example configuration contains placeholders only.
- Test and development provider keys use restricted projects and budgets.
- Containers receive only the credentials they need.

### Public deployment

- Use the hosting platform's managed secret store and least-privilege service identities.
- Encrypt traffic in transit and persistent storage at rest.
- Rotate credentials after exposure, personnel change, or according to provider policy.
- Never place secrets in images, build arguments, frontend bundles, or repository history.

### Logging and tracing

- Structured logs use request, job, workspace, and trace identifiers.
- Raw document text and full prompts are excluded by default.
- An explicit local-development API override may emit only failed answer-generation or
  statement-support model output for approved public or synthetic material. It is disabled by
  default, must never run in CI or shared/public environments, and is not sent to browser errors or
  normal traces. Local Docker logs can retain this output; disable the override after diagnosis.
- Error messages, filenames, query text, and model traces follow redaction and sampling rules.
- LangSmith is optional for request execution and never the only audit store.
- Product audit events record actor, workspace, action, target, time, and safe change metadata.
- Authentication, import, reprocessing, deletion, restore, export, configuration, and administrative actions are auditable.

## 8. Reliability and asynchronous work

- PostgreSQL records durable job intent; Redis contains temporary delivery state.
- Queue delivery is assumed to be at least once, so workers are idempotent.
- Jobs use deterministic identifiers, bounded retries, exponential backoff, jitter, and a terminal failure state.
- Retries distinguish transient provider failures from permanent validation or parsing failures.
- A failed optional enrichment stage cannot make a searchable document unavailable.
- Canonical changes and outbox events are committed in one database transaction.
- Vector updates use deterministic identifiers and recorded receipts.
- Reconciliation detects missing, stale, and orphaned vector records.
- Circuit breakers or temporary degradation prevent repeated provider failures from exhausting resources.
- LangSmith failure must not fail an otherwise valid product request.

## 9. Retention, deletion, backup, and recovery

### Deletion

- The owner-approved local deletion path is permanent, without application restoration; see
  [ADR-0019](./adr/0019-permanent-local-document-deletion.md) for the retention matrix and gates.
- Canonical `PURGE_PENDING` immediately excludes a document from browsing, retrieval, and new answers.
- Purge removes original objects, vectors, canonical source content, and complete affected stored answers.
- Content-free deletion receipts and detached provider accounting remain; deletion must not reset budgets.
- Derived vectors, processing artifacts, cached data, and provider-held data are included in deletion workflows.
- Affected backups expire within 35 days unless a documented hold applies.
- Purge jobs are idempotent and leave an auditable completion result without retaining deleted content unnecessarily.
- The backend purge service verifies external absence before atomic canonical removal. Database
  guards serialize source writes with deletion admission and reject writes after exclusion.
- Exact-key S3 cleanup covers the unversioned local store, not historical versions in a future
  versioned cloud bucket. The isolated queue-recovery gate verifies live SeaweedFS cleanup; live
  Pinecone deletion still requires its own scoped verification.
- ID-only worker jobs enforce trusted workspace identity, bounded scheduled retries, and
  per-attempt time limits. Retry exhaustion leaves durable pending intent; it never implies
  completion or restoration. Durable outbox delivery and periodic reconciliation remain M2 work.
- The API requires an explicit bounded ID batch and boolean permanent-deletion confirmation, then
  commits exclusion before broker publication. Content-free status reads remain workspace-scoped
  after physical removal. A broker failure does not restore content or claim cleanup completion;
  resubmission retries pending publication. The website requires a second explicit confirmation,
  clears the old answer view, and never equates acceptance or a missing response with completion.
  Bounded status reads and same-batch manual retry are implemented; page-local tracking and its
  reload limitation are visible. Manual UI/build and live Pinecone purge verification remain open.
  This local policy does not approve public or multi-user retention; review it before such deployment.

### Local runtime

- Docker volumes are development convenience, not a sufficient backup strategy.
- Back up PostgreSQL and original objects before destructive migrations or important test-data changes.
- Keep a documented command and verification procedure for database restore and vector-index rebuild.

### Public deployment

- Automate encrypted database backups and enable point-in-time recovery where available.
- Use versioned object storage with lifecycle and deletion rules.
- Test restoration rather than assuming backups are usable.
- Store active parser, chunk, embedding, and index manifests in canonical state.
- Treat Redis loss as recoverable queue/cache loss, not canonical data loss.
- Rebuild the vector index from active canonical chunks and version metadata.

Provisional hosted recovery objectives are an RPO of 24 hours and an RTO of 4 hours. They are engineering targets, not user-facing guarantees, until measured in recovery exercises.

## 10. Observability and cost control

Collect four distinct signal types:

| Signal | Purpose |
|---|---|
| Product audit | Who changed or accessed product state |
| Logs | Diagnose application and worker events |
| Metrics | Track rates, latency, errors, queues, resources, and cost |
| Traces | Follow one import or research request across services and AI calls |

Minimum operational views:

- API request rate, latency, and error rate;
- queue depth, oldest-job age, retries, and terminal failures;
- processing duration and failures by stage and parser version;
- lexical, dense, fusion, reranking, generation, and validation latency;
- evidence-validation failures and insufficient-answer rate;
- provider errors, rate limits, token usage, and estimated cost;
- vector-index drift and reconciliation repairs;
- deletion backlog and backup/restore status after hosting.

The provisional M1 AI-provider budget target is **EUR 5 per month**. Because the approved provider
prices and canonical ledger use US dollars, the local M1 runtime enforces a fixed hard ceiling of
**USD 5 per workspace and UTC month** (`5,000,000` micro-USD). This fixed ceiling deliberately avoids
runtime foreign-exchange dependencies and must be reviewed before the provider billing currency,
workspace model, or deployment scope changes. It does not claim that later hosted infrastructure can
fit inside the same amount. Record attributable usage for each AI operation and configure matching
provider-side spending limits where available. Add hosted alerts before public release; avoid
building a complex alerting stack for local-only development.

## 11. Delivery and change safety

The automated pipeline should progressively add:

1. formatting, linting, and type checks;
2. unit and schema-contract tests;
3. PostgreSQL, queue, and object-storage integration tests;
4. isolated vector and AI-provider adapter tests;
5. dependency, secret, and container vulnerability scanning;
6. database migration safety checks;
7. malicious-file, prompt-injection, workspace-isolation, and evidence tests;
8. a small retrieval and answer regression suite;
9. public-environment smoke tests and rollback checks when hosting exists.

Lock dependencies and review upgrades. Model, prompt, embedding, parser, chunking, and reranker changes are versioned configuration changes and receive the same review discipline as code.

Infrastructure as code is introduced with the selected public hosting platform; it is not required to complete the initial local vertical slice.

## 12. Incident response and runbooks

For a security or reliability incident:

1. contain the affected account, provider key, import, worker, or deployment;
2. preserve safe logs, audit records, and version identifiers;
3. determine affected workspaces, documents, answers, and time range;
4. revoke or rotate exposed credentials;
5. restore canonical service and rebuild derived state where required;
6. verify evidence, isolation, deletion, and index consistency;
7. document cause, impact, corrective actions, and regression tests.

Maintain short runbooks for:

- provider outage or rate limiting;
- stuck queue or repeated job failure;
- malicious or unparseable document;
- database restore;
- vector-index rebuild and drift repair;
- failed migration or deployment rollback;
- permanent-deletion confirmation, interrupted cleanup, and backup-expiry boundaries;
- suspected cross-workspace or credential exposure.

## 13. Provisional service objectives

These targets guide testing and may change after real measurements:

| Indicator | Initial objective |
|---|---|
| Archive-derived statement citation coverage | 100% |
| Cross-workspace isolation tests | 100% pass |
| Import request acceptance | p95 within 2 seconds |
| Search-only latency | p95 within 2 seconds |
| Direct-RAG time to first user-visible output | p95 within 3 seconds |
| Normal 50-page text-PDF searchable | p95 within 5 minutes |
| Hosted API server-error rate | Below 1% excluding rejected invalid requests |
| Hosted recovery point objective | At most 24 hours |
| Hosted recovery time objective | At most 4 hours |

Citation and workspace-isolation objectives are hard correctness gates. Latency and recovery values are provisional engineering hypotheses until measured.

## 14. Release gates

### Before the complete local vertical slice is accepted

- Workspace authorization and negative tests pass.
- Supported-file and processing-resource limits are enforced.
- Prompt-injection and invalid-model-output fixtures fail safely.
- Duplicate delivery, retry, reprocessing, deletion, and stale-index tests preserve canonical state.
- Archive-derived statements cannot bypass evidence validation.
- Secrets are absent from source control, images, logs, and frontend code.
- PostgreSQL restore and vector-index rebuild procedures have been exercised locally.

### Before a public demo is released

- Only sanitized demo data is present.
- HTTPS, authentication, read-only demo authorization, rate limits, and security headers are enabled.
- Hosted secrets, backups, restore testing, logging, metrics, alerts, and cost limits are configured.
- Environment separation and least-privilege provider credentials are verified.
- Dependency and container scans have no unresolved critical findings.
- Deployment and rollback smoke tests pass.
- The versioned pre-demo evaluation passes its agreed critical thresholds for retrieval, answer support, citation correctness, unanswerable questions, temporal behavior, latency, and cost.
- Privacy notice, retention behavior, and external-provider data flow match actual behavior.
