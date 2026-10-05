# Investment Research Archive — M1.5 RAG Quality Proof Plan

**Status:** Active — Step 0 in progress; no M1.5 quality baseline recorded yet
**Date:** 2026-10-01  
**Milestone:** M1.5 — RAG quality proof, after accepted M1 and before M2

**Sources of truth:** [Delivery plan](./05-delivery-plan.md),
[M1 contracts](./11-m1-contracts.md), [system design](./02-system-design.md),
[domain model](./03-domain-model.md), [security and operations](./04-security-and-operations.md),
and accepted [ADRs](./adr/README.md). This plan advances selected M4 RAG work without claiming
that the full M4 gate is complete.

## 1. Outcome and boundary

The accepted M1 path already answers questions over saved, eligible PDF and pasted-text documents
with lexical and dense retrieval, typed generation, statement-support evaluation, and exact
original-source citations. M1.5 makes that path more robust and measurable for a local portfolio
demonstration. It does not turn the application into a public or production service.

The implementation order is M1 → M1.5 → M2 → M3 → remaining M4 work. M2 still owns durable
delivery, versions, reprocessing, deletion, and index reconciliation. M3 still owns archive
browsing, company metadata, filters, and a broader discovery/evaluation core. M4 still owns the
complete routed, conversational, temporal, and production-shaped direct-RAG gate.

Only approved public, synthetic, openly licensed, or otherwise approved non-sensitive material
may reach external providers. Fixed local identity and loopback-only access remain in force. No
new production dependency, paid provider, or hosted tracing service is approved by this plan.

## 2. Baseline and design rules

M1's selector alternates eligible PostgreSQL lexical and canonically qualified Pinecone dense
hits, deduplicates chunk IDs, and passes at most eight chunks and 24,000 code points to generation.
It has no RRF or reranking. The generator currently proposes a candidate ID and verbatim substring;
the server resolves offsets and rejects a missing or ambiguous quote. Luna's observed
`quote_not_found` failures make quote selection a specific M1.5 test case, not permission to
weaken validation.

- Preserve the M1 API response and citation-viewer contract unless a reviewed contract change is
  required. A selected model must never make an unsupported statement visible.
- Keep PostgreSQL and immutable originals canonical. Pinecone scores and model-selected IDs are
  untrusted suggestions, not evidence or authorization.
- Apply workspace, current-version, lifecycle, processing, configuration, and temporal eligibility
  before fusion, reranking, or generation, then revalidate evidence before answer publication.
- Keep deterministic correctness tests separate from measured retrieval and answer quality.
- Version every evaluated corpus, query, retrieval policy, citable-unit policy, reranker,
  prompt, model pair, and result manifest. Do not silently re-interpret M1 records.
- Compare added quality with latency, cost, and failure rates. Keep the M1 baseline available for
  an honest before/after comparison; do not adopt a more complex component without evidence.

## 3. Work sequence

Each step follows the repository order: backend behavior and tests, required CI, frontend only
where the step needs it, then the complete step gate. Work in coherent reviewable blocks and pause
between blocks. All required CI tests use deterministic adapters and spend no provider credits.

### Step 0 — Freeze the evaluation contract and starting baseline

Define a versioned, reviewed, redistributable corpus and question manifest before tuning ranking
or prompts. Include PDF and pasted text, multiple documents, exact-word and paraphrased questions,
answerable and unanswerable cases, a direct source conflict, repeated wording, Unicode, and a
question that exposes the known Luna quote failure. Each case records approved source spans or
explicit expected non-answer behavior; reviewers must not infer truth from the model's output.

Record the current M1 policy, fixture provenance and permitted provider exposure, model pair,
corpus/query versions, configuration, and descriptive baseline results. Select critical
correctness gates and quality measurements before comparing candidates. At minimum measure
candidate recall at the context budget, answer support, exact citation validity, appropriate
abstention/conflict handling, end-to-end latency, provider usage, estimated cost, and safe failure
rate. Small-sample statistics are descriptive, not population-level quality claims.

**Gate:** every case and expected evidence range is reviewed, the deterministic baseline is
reproducible offline, and any live baseline is separately opt-in, bounded, and recorded without
raw document text or secrets in logs, traces, or CI artifacts. No RRF tuning precedes this gate.

**Current Step 0 status (2026-10-05):** The owner reviewed the six synthetic cases in
[`fixtures/m1_5/evaluation.json`](../fixtures/m1_5/evaluation.json). The fixture digests and original
source ranges have a credential-free validator. Separate retrieval-only and end-to-end observation
scorers have deterministic unit tests, and canonical chunks and citations can be mapped to reviewed
source passages. An isolated PostgreSQL run now supplies the first retrieval-only M1 baseline:
required reviewed evidence was available in context for 5/5 answerable cases, using deterministic
embedding and SQLite vector adapters. Its measured median retrieval time was 6.5 ms on one local
run, not a provider latency estimate. See the [corpus guide](../fixtures/m1_5/README.md) for the
reproduction command and limitations. A separate credential-free contract test now exercises all
six reviewed cases against the canonical evidence validator with scripted proposals: exact
quotes, conflict and abstention shapes are accepted, while an altered quote and a rejected support
decision fail closed. This is a validator correctness check; its support decision is fixed by the
test. A complete offline application run now exercises retrieval through canonical answer
persistence and readback: 6/6 scripted coverage and citation outcomes match, including one conflict
and one abstention, and two injected invalid proposals persist `REJECTED` without published
content. Its first local median was 47 ms and maximum 71 ms, including passage mapping. The
existing PostgreSQL CI job discovers these tests automatically. The generator and support adapter
use reviewed fixture rules, so independent support assessments, provider usage, cost, and live
model quality remain unmeasured. The offline correctness baseline is reproducible; the full
Step 0 gate remains open pending the separately opted-in quality measurements.

A credential-free live preflight now pins this reviewed corpus digest and reports the existing
M1 configuration and independently selected Terra/Luna model prices. Its sequential invocation
guard reserves attempted calls before access, retains unknown-usage reservations, validates
reported usage, and enforces role request limits and an OpenAI accounting ceiling of at most
USD 0.30. The [corpus guide](../fixtures/m1_5/README.md#credential-free-live-preflight) documents the
verified command, estimates, and limitations. It does not contact providers, authorize spend,
replace canonical application accounting, or record a live quality baseline. Budgeted wrappers
for all four provider roles and an owned Pinecone namespace lifecycle now have deterministic unit
coverage. The lifecycle limits unique vectors, applies total visibility and cleanup deadlines,
and reports failed cleanup while closing both index clients. These helpers do not create live
clients or execute a paid run by themselves. Connecting them to canonical research, explicit
opt-in execution, and complete observations is the next Step 0 block; independent support review
and live quality measurements remain pending.

### Step 1 — Reciprocal rank fusion of the existing two lanes

Add an application-owned selector that fuses lexical and dense _ranks_, not incomparable raw
scores. Use a documented fixed RRF constant, deterministic tie-breaking, chunk-ID deduplication,
and the existing context budget. Keep lane-origin ranks and raw scores for audit; record the new
fusion policy/version and fused rank separately from M1 selection rank. Canonical qualification
must finish before a hit can contribute to fusion.

Test one-lane-only hits, hits in both lanes, ties, duplicates, empty lanes, stale/foreign vectors,
and budget boundaries. Compare the same reviewed cases with M1 round-robin and RRF without
changing embeddings, corpus, or prompts between runs.

**Gate:** all M1 correctness and isolation tests still pass. The RRF comparison is reproducible,
and the chosen default is justified by measured quality without critical evidence regressions.
If RRF does not help, retain M1 as the default and document the result.

### Step 2 — Model-selectable citable source units

Design a bounded set of pre-generation citable units derived deterministically from eligible
canonical `StructureBlock` text. Give each unit an opaque request-scoped ID and exact
block-relative Unicode-code-point range. The model may select unit IDs for a statement instead of
copying `exact_text`; the server resolves each ID to immutable original text and the existing PDF
page or pasted-text locator. An ID does not itself authorize support: the statement-support
evaluator and canonical evidence checks remain mandatory.

Freeze the unit-boundary and ID contract before code changes. Do not store a new `EvidenceSpan`
merely because a unit was offered to a model, and do not replace stable original-document anchors
with chunk or vector IDs. Preserve historical M1 answers and the source viewer. Test repeated
phrases, whitespace, line breaks, Unicode, overlapping/adjacent units, unknown IDs, mixed or
cross-workspace IDs, changed eligibility, and multiple supporting units.

**Gate:** exact citations still open the original location, invalid IDs fail closed, and the
reviewed Luna quote-failure cases are compared against the prior exact-substring contract. No
fuzzy match or whole-chunk citation is accepted as a workaround.

### Step 3 — Bounded reranking experiment

Research a maintained multilingual cross-encoder or provider behind a small typed adapter.
Before adding a dependency or paid service, review security, license, data handling, Python 3.14
compatibility, release age, runtime resources, latency, and cost with the user. Keep a no-rerank
path. Reranking receives only canonically eligible, bounded candidates, and its output cannot
introduce an unqualified source or relax the evidence contract.

Compare M1, RRF, and RRF-plus-rerank on the same frozen cases. Include timeout, invalid score,
provider outage, and budget tests. A reranker is adopted only when measured benefit justifies
added complexity and latency; a negative result is a valid documented outcome.

**Gate:** deterministic CI passes without the real reranker, live checks remain separate and
bounded, and the selected policy has a recorded quality/latency/cost comparison.

### Step 4 — Quality hardening and portfolio evidence

Exercise the four existing coverage states (`COMPLETE`, `PARTIAL`, `CONFLICTING`,
`INSUFFICIENT`) across both available answer/support-model choices. Review failures by stage and
reason without making raw provider output a normal log or UI feature. Fix narrowly demonstrated
retrieval, selection, prompt, or validation issues and rerun the frozen cases after each change.

Publish a concise, reproducible local architecture explanation, versioned evaluation summary,
before/after examples, costs, known limitations, and a safe demo script using only approved
documents. The user may capture screenshots or run the browser journey themselves; public hosting
remains M6, not an M1.5 deliverable.

**Gate:** the complete M1 runtime and source-opening gates pass; no critical workspace,
eligibility, citation, abstention, or provider-failure regression remains; results and limitations
are reproducible and accurately described. This is a local RAG quality proof, not an M2 reliability
gate or the full M4 release gate.

## 4. Handoff and stop rule

Stop M1.5 when its reviewed correctness gates pass and the chosen retrieval/citation approach has
one reproducible baseline comparison with quality, latency, and cost. Do not extend the phase
indefinitely to chase every model failure or metric. Record unsupported or inconclusive variants
and hand off to M2; expand the corpus and discovery measurements in M3; finish routing,
conversation, temporal modes, tracing/export decisions, and the full direct-RAG gate in M4.

The first implementation block after this plan is the evaluation contract and baseline. No new
ranking or model dependency should be introduced before those cases are reviewed.
