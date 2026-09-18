# ADR-0008 — Application-owned AI and vector adapters

**Status:** Accepted

**Date:** 2026-09-17

**Scope:** M1 embeddings, dense indexing, answer generation, semantic support evaluation, and local
trace context

## Context

M0 proves the evidence-grounded archive with PostgreSQL lexical retrieval and deterministic
adapters. M1 must add the first complete dense-retrieval and provider-backed answer path without
making an external model, vector database, framework, or tracing service authoritative.

The application already owns typed ports for embeddings, dense retrieval, answer generation,
statement-support evaluation, and operation tracing. The provider choice must preserve those
boundaries, keep PostgreSQL and object storage canonical, and ensure that every proposed citation
is revalidated against eligible original-document text before it becomes product output.

M1 is a local single-user slice. Only public, synthetic, openly licensed, or otherwise approved
non-sensitive fixture content may cross an external provider boundary. Authentication, private-data
approval, production observability, and public deployment remain later milestones.

## Decision drivers

- prove semantic retrieval and typed provider generation with the smallest credible external stack;
- keep provider SDK types outside domain and application modules;
- preserve deterministic, offline tests for correctness and isolation;
- keep vector data derived, workspace-separated, reproducible, and canonically qualified;
- use strict structured output rather than parsing unconstrained prose;
- bound provider input, output, retries, timeouts, concurrency, and cost;
- prevent raw documents, prompts, questions, or model responses from entering logs and traces;
- retain an explicit path to replace models, vector stores, or orchestration later;
- avoid adding a RAG framework before measured workflow complexity justifies it.

## Options considered

1. **OpenAI embeddings and generation, Pinecone BYOV, and application-owned adapters.** Selected.
   This provides one maintained model API, a managed dense index, structured output, and a narrow
   implementation behind the existing ports.
2. **Pinecone integrated embedding.** Rejected for M1 because embedding ownership, model versioning,
   input policy, usage accounting, and failure handling would move behind the vector-store boundary.
   The application must create and version its own vectors.
3. **A self-hosted embedding and generation stack.** Deferred because model serving, hardware,
   patching, evaluation, and capacity management would dominate the first RAG slice without
   improving its architectural proof.
4. **OpenAI File Search, hosted vector stores, or provider-managed RAG.** Rejected because they
   duplicate application retrieval, obscure canonical qualification, and create another content
   store outside the accepted ownership model.
5. **LangChain, LlamaIndex, or LangGraph as the orchestration core.** Rejected for M1 because the
   current path is linear and already represented by small typed services. LangGraph may be
   reconsidered in M9 only if measured multi-step cases satisfy the admission gate.
6. **Keep deterministic adapters only.** Rejected because M1 must demonstrate real semantic
   retrieval and provider-backed structured generation, not only the M0 proof path.

## Decision

Domain and application modules depend only on immutable application contracts such as
`EmbeddingProvider`, `DenseRetriever`, `AnswerGenerator`, `StatementSupportEvaluator`, and
`OperationTracer`. OpenAI, Pinecone, and OpenTelemetry imports are restricted to infrastructure and
composition code.

Provider responses are untrusted input. Adapters translate them into strict application-owned value
objects, validate shape and bounds, and raise safe application errors. They never return provider
SDK objects to application services. One operation has one explicit retry owner; SDK-internal and
application retries must not combine into an undocumented attempt count.

The selected M1 configuration is:

| Capability                    | Selection                                                   | Required boundary                                                                                |
| ----------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Document and query embeddings | OpenAI `text-embedding-3-small`, full 1536 dimensions       | Ordered, finite, dimension-checked results; separate versioned document and query configurations |
| Answer proposal               | OpenAI `gpt-5.6-terra`, Responses API, low reasoning effort | Strict Structured Outputs, foreground stateless request, `store=false`, no tools                 |
| Statement-support evaluation  | OpenAI `gpt-5.6-terra`                                      | Separate prompt, schema, timeout, usage, and configuration version                               |
| Dense index                   | Pinecone Serverless on-demand BYOV in `aws/us-east-1`       | 1536-dimensional cosine index; one server-derived namespace per workspace                        |
| Trace context                 | OpenTelemetry Python API and SDK with W3C propagation       | No exporter, Collector, hosted backend, or LangSmith in M1                                       |

Terra is selected over the flagship tier because the bounded RAG task does not justify flagship
cost, and over Luna because evidence-sensitive financial answers favor the stronger balanced tier.
A measured quality suite may later justify a cheaper support model.

The following rules are binding:

- OpenAI receives only the bounded question and selected canonical candidate text. Requests contain
  no tools, background mode, provider-hosted file search, or persistent conversation state.
- The model may propose statements and references but cannot authorize evidence. The server
  rechecks canonical workspace, version, state, time, exact text, offsets, and support before output.
- A model or embedding-dimension change creates a new configuration version and vector identity;
  it never silently changes an existing configuration.
- Pinecone stores deterministic vector IDs plus bounded identifiers and versions, never raw text,
  questions, prompts, answers, filenames, user metadata, or credentials.
- Pinecone is derived and eventually consistent. Every result is rehydrated and qualified through
  PostgreSQL; stale, unknown, deleted, wrong-configuration, or foreign hits are discarded.
- Product requests use only a configured Pinecone data-plane host. Resource provisioning and
  cleanup remain separate, narrowly scoped operational actions.
- `aws/us-east-1` is allowed only for public or synthetic M1 fixtures on the Starter plan. Region and
  provider are reconsidered before private data, shared use, or public deployment.
- OpenTelemetry attributes use a strict safe allowlist. PostgreSQL remains canonical for provider,
  model, configuration, usage, estimated cost, outcome, correlation, and trace identifiers.
- Normal tests use deterministic adapters with no network, credentials, or provider spend. Live
  checks are opt-in, fixture-bound, cost-bounded, and self-cleaning.
- Credentials reach only the required process and never browser code, queue payloads, parser input,
  fixtures, logs, traces, or committed environment files.

The approved package baselines are `pinecone` 10.0.0, `openai` 3.14.1 or a later compatible 3.x,
and `opentelemetry-api` plus `opentelemetry-sdk` 1.44.0 or a later compatible 1.x. Exact versions are
locked in `backend/uv.lock` only after another release-age, provenance, Python 3.14, license,
transitive-dependency, and vulnerability check. OpenAI 3.14.1 is not eligible before
2026-09-22 23:12 UTC and receives no exception merely to start M1.

Pinecone 10.0.0 was locked on 2026-09-18 after its release-age, PyPI Trusted Publishing
provenance, Apache-2.0 license, CPython 3.14 import, ARM64 wheel availability, transitive dependency,
malware, and vulnerability checks passed. The M1 data adapter uses the gRPC client directly with TLS,
a 20-second per-operation deadline, concurrency one, and `RetryConfig(max_retries=0)`. The REST data client is not
used because version 10.0.0 does not forward configurable retry policy to REST data-plane calls;
using it would violate the single application-owned retry policy.

OpenAI API data is not used for training by default, but default abuse-monitoring retention may
contain content for up to 30 days. M1 therefore permits only approved public or synthetic content.
`store=false` is required but is not treated as Zero Data Retention approval. The frozen fixture
manifest sets the live-smoke cost ceiling before any credentialed call; normal CI spends zero.

## Consequences

M1 gains a credible real dense-retrieval and generated-answer path while preserving the archive's
canonical and evidence boundaries. Provider replacement remains adapter and configuration work
rather than a domain rewrite. Deterministic tests stay fast, offline, and reproducible.

The application now depends operationally on OpenAI and Pinecone availability for the live M1 path.
Provider failure must therefore remain visible and safe rather than being hidden with unsupported
answers. Pinecone's eventual consistency requires bounded readiness behavior and canonical
qualification. OpenAI and Pinecone introduce cost, regional, retention, rate-limit, and vendor
lock-in considerations that must be measured and revisited before private or public use.

Using Terra for both generation and support evaluation favors initial correctness over minimum
cost. The quality suite may later justify a cheaper support model. Using `us-east-1` favors the M1
Starter path over EU residency and is forbidden once the approved public/synthetic data restriction
no longer applies.

No external trace backend means M1 proves trace propagation and canonical usage accounting but not
production trace collection. M4 decides LangSmith or another exporter only after the direct-RAG
baseline demonstrates a concrete observability need.

## Validation and reconsideration triggers

Before M1 is complete, tests must prove:

1. embedding batches preserve correlation and reject missing, extra, wrong-dimension, or non-finite
   vectors;
2. deterministic vector IDs make duplicate indexing idempotent;
3. every Pinecone operation names the trusted workspace namespace explicitly;
4. stale, unknown, wrong-configuration, deleted, or foreign vector hits never reach generation;
5. generation and support output satisfy strict schemas and reference only supplied candidates;
6. no answer statement or citation is emitted before complete canonical evidence validation;
7. provider timeout, refusal, rate limit, malformed output, and outage produce bounded safe failure;
8. logs, traces, vector metadata, errors, and CI artifacts contain no injected secret or raw-content
   markers;
9. required CI passes with deterministic adapters and no provider credentials;
10. opt-in live smoke tests stay within their fixture manifest, cleanup scope, and cost ceiling;
11. loss or disabling of trace export does not affect canonical usage records or valid answers;
12. dependency locks and audits show no applicable known vulnerability before merge.

Reconsider this decision if representative evaluation shows inadequate embedding recall, Terra
cannot meet the typed evidence contract, provider retention becomes incompatible with intended data,
Pinecone qualification or isolation cannot be proven, provider costs exceed the approved budget, or
a measured multi-step workflow meets the M9 framework-admission criteria.

## References

- [OpenAI `text-embedding-3-small`](https://developers.openai.com/api/docs/models/text-embedding-3-small)
- [OpenAI `gpt-5.6-terra`](https://developers.openai.com/api/docs/models/gpt-5.6-terra)
- [OpenAI Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
- [OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data)
- [OpenAI Python SDK releases](https://github.com/openai/openai-python/releases)
- [Pinecone create-index API and regions](https://docs.pinecone.io/reference/api/2026-04/control-plane/create_index)
- [Pinecone indexing and namespaces](https://docs.pinecone.io/guides/index-data/indexing-overview)
- [Pinecone upsert semantics](https://docs.pinecone.io/guides/index-data/upsert-data)
- [Pinecone Python SDK](https://pypi.org/project/pinecone/)
- [Pinecone Python retry behavior](https://sdk.pinecone.io/python/guides/retries.html)
- [OpenTelemetry Python](https://opentelemetry.io/docs/languages/python/)
- [OpenTelemetry manual instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)
