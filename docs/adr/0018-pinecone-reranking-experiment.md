# ADR-0018 — Bounded Pinecone cross-encoder reranking experiment

**Status:** Accepted experiment; implemented and evaluated; not adopted as a runtime default

**Date:** 2026-10-08

**Scope:** Opt-in reranking of canonically eligible public/synthetic passages after RRF

## Context

M1.5 already has an application-owned RRF selector and server-resolved citable source units.
Step 3 compares that path with a bounded cross-encoder reranker. The owner selected Pinecone's
hosted `bge-reranker-v2-m3` after reviewing local serving, hosted inference, and OpenAI's
provider-managed retrieval alternative. This approves the provider boundary, not a quality claim,
a paid-plan upgrade, a live execution, or a new runtime default.

[ADR-0008](./0008-application-owned-ai-and-vector-adapters.md) currently limits Pinecone product
calls to the configured index host and prohibits raw text in vector metadata. Standalone inference
requires an explicit additional endpoint and sends passage text, not just vectors.

## Decision drivers and alternatives

- Keep retrieval, canonical qualification, source selection, and evidence validation application-owned.
- Test a multilingual relevance model without adding a local GPU/model-serving stack.
- Preserve the no-rerank comparison and measure quality, latency, failure rate, and usage.
- Keep provider calls bounded and exclude private data and raw-content logs.

Local BGE serving remains a replacement option but adds model downloads, inference dependencies,
hardware sizing, and patching. GPT relevance scoring is a separate LLM-reranking experiment, not
this cross-encoder implementation. Provider-managed RAG remains outside scope.

## Decision

Use Pinecone Inference's standalone `bge-reranker-v2-m3` behind the existing application-owned
`Reranker` port. Reuse the locked Pinecone SDK if its inference transport can enforce the limits
below; do not upgrade it or add a production dependency without a separate reviewed approval.
The upstream BAAI model is multilingual and Apache-2.0 licensed. Hosted service terms are a
separate boundary; the model license alone does not establish privacy or retention guarantees.

The experiment follows:

```text
Canonical lexical/dense candidates → RRF → bounded reranking → context selection
    → citable source units → answer proposal → canonical evidence/support validation
```

### External data and credentials

- Allow only the standalone TLS endpoint `https://api.pinecone.io/rerank` in addition to the
  existing index-host operations. No provisioning, provider-managed search, embedding, or storage
  operation is added by this decision.
- Send the bounded question, eligible passage text, model/options, and request-local correlation
  handles. Do not send workspace identifiers, canonical document IDs, filenames, object URLs,
  credentials in payload fields, answers, or unrelated metadata.
- Keep canonical text in PostgreSQL/original storage and out of Pinecone vector metadata. Sending
  text for inference is not permission to persist it in the index or a guarantee that the provider
  retains nothing.
- Permit only explicitly approved public or synthetic content. Do not infer inference residency
  from the vector index's region. Retention, training, subprocessors, region, and contractual
  controls must be reviewed before any private, shared-network, or public deployment use.
- A process-specific credential reaches only the opt-in API/evaluation process. Verify the
  required inference permissions before live use; do not broaden existing indexing credentials
  silently. No credential reaches the browser, worker jobs, parser, logs, or fixtures.

This narrowly extends ADR-0008's index-host-only rule for the experiment. Its canonical-store,
vector-metadata, workspace-isolation, and other provider rules remain binding.

### Bounds and response validation

- Qualify and deduplicate candidates canonically before transmission. Send at most the existing
  contract ceiling of 100 candidates, with a smaller explicit experiment bound and total input
  bound frozen before integration. Keep the generation context's eight-source and 24,000-code-point
  limits unchanged.
- Enforce one in-flight rerank operation per execution, a deadline of at most 20 seconds, and no
  hidden SDK or application retries. Verify the inference transport independently of the existing
  gRPC index adapter; index-adapter tests do not prove inference behavior.
- Verify the hosted query/passage limits and truncation controls before integration. Do not
  silently truncate sources or discard over-limit candidates. A changed passage/window policy
  requires its own version and evaluation; unsupported input fails safely.
- Request a result for every submitted candidate with document echo disabled. Map returned input
  positions through the server-owned request mapping. Reject missing, duplicate, foreign, or
  out-of-range positions, unexpected model identifiers, non-finite scores, and malformed usage.
- Return a complete unique ranking over the supplied candidates. Use original RRF order for
  equal scores. A relevance score is neither a truth probability nor evidence authorization.
- Rerank failure is a visible, classified experimental failure, not an unrecorded fallback or
  automatic paid retry. The explicitly selected no-rerank path remains available.

### Usage, reproducibility, and adoption

Pinecone's pricing page, checked on 2026-10-08, lists 500 monthly BGE reranking requests in Starter.
That quota is account-wide provider usage, not a locally guaranteed allowance. Tests consume it
too. Do not change plans, assume unused quota, or disable application budgets because of it.
Before any owner-run live comparison, freeze the verified account/quota conditions, request and
spend ceilings, price policy, and usage accounting. Keep existing monthly and evaluation budget
limits intact; record reranking separately from OpenAI token charges.

Version the application reranker configuration, request limits, truncation policy, and selection
policy. Record the requested and returned hosted model name and comparison date. A stable provider
model name does not prove immutable weights or bit-for-bit reproducibility; do not claim that the
hosted model was pinned to the upstream repository revision.

Normal tests remain offline and credential-free. Compare M1, RRF, and RRF-plus-rerank using the same
frozen corpus, questions, source-unit policy, answer/support model pair, and generation bounds.
Change only the ranking policy in the paired comparison. Adoption requires reviewed improvement
that justifies additional latency, usage, and failures; a negative or inconclusive result is valid.

## Consequences and verification gate

The experiment adds one external question/text boundary and a provider availability/quota
dependency, but no canonical store, local inference runtime, migration, or RAG framework.
The original decision approved implementation only. The adapter and bounded evaluation runner
are now implemented. The subsequent six-case RRF/no-rerank versus RRF/BGE comparison recorded
zero execution failures and verified cleanup for both runs, but no measured quality benefit
justifying default adoption. Protocol, scoring limits, and the separate approval of those live
runs are recorded in [M1.5 Step 3](../13-m1-5-rag-quality-plan.md#step-3--bounded-reranking-experiment).
Reranking remains opt-in; this status update approves no new paid execution or runtime change.

Before integration, deterministic tests must prove eligibility, input bounds, timeout/retry
behavior, complete result correlation, finite scores, safe errors, budget/usage handling, and
content-free logging. Before adoption, record the live quality/latency/cost comparison and retain
source-opening and full M1.5 correctness gates. Reconsider the provider if its transport, privacy,
model limits, availability, or quota cannot satisfy these constraints.

## References

- [M1.5 Step 3](../13-m1-5-rag-quality-plan.md#step-3--bounded-reranking-experiment)
- [Pinecone standalone reranking](https://docs.pinecone.io/guides/search/rerank-results#standalone-reranking)
- [Pinecone BGE model](https://docs.pinecone.io/models/bge-reranker-v2-m3)
- [BAAI model and license](https://huggingface.co/BAAI/bge-reranker-v2-m3)
- [Pinecone pricing](https://www.pinecone.io/pricing/)
