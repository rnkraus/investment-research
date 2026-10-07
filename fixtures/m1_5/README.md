# M1.5 RAG evaluation corpus

`evaluation.json` is the reviewed, versioned question catalog for the M1.5 quality baseline. It
reuses the frozen M1 PDF and pasted-text fixtures and adds one synthetic operations note. All three
documents are fictional and redistributable; the new note is **not** automatically imported into
the ordinary M1 runtime.

Run the credential-free structural check from `backend/`:

```bash
uv run --locked python -m scripts.verify_m1_5_corpus
```

The check validates file digests, PDF page and pasted-text character ranges, unique IDs, document
scope, and evidence references. It does **not** run retrieval, call a model, calculate quality
scores, or decide whether the expected answer is factually appropriate. The optional
`--require-reviewed` flag enforces the owner's review decision recorded in the catalog.

## Opt-in support attribution experiment

The live evaluator defaults to `--support-policy m1` (exact quotes only). To compare the bounded
same-block attribution profile, explicitly add `--support-policy attribution` to a separately
authorized live invocation. This choice is independent of `--retrieval-policy` and
`--answer-policy`; for the source-unit experiment use `--retrieval-policy rrf --answer-policy
citable_units`. Without `--execute`, the same options produce a credential-free preflight plan:

```bash
uv run --locked python -m scripts.verify_m1_5_live_baseline \
  --retrieval-policy rrf \
  --answer-policy citable_units \
  --support-policy attribution \
  --answer-model gpt-5.6-luna \
  --support-model gpt-5.6-luna
```

Each exact quote retains its own original document/version/block identity and receives a separate
bounded original-block prefix for company/report/date attribution only. Context cannot supply
missing operational facts, expand a stored citation, or create extra evidence spans. The full
payload is budgeted and rechecked before storage. Normal runtime and website behavior are unchanged;
see the [M1.5 contract](../../docs/13-m1-5-rag-quality-plan.md) for the complete restrictions.

The report records the distinct support prompt version but no raw context. With the existing
`--show-local-model-output` option, the interactive local terminal additionally displays each exact
quote, its separately paired context, and the evaluator's bounded decision reason. This output is
for the approved public-synthetic corpus only, is forbidden in CI or redirected output, and is not
independent support review. Use a fresh ignored report filename for each paid run and retain the
USD 0.80 default maximum reservation unless explicitly approving a different bound.

| Case | Intended distinction |
| --- | --- |
| FY2025 revenue | Exact wording and an original PDF page citation |
| FY2026 rollout constraint | Paraphrase over pasted text |
| FY2026 dividend | No supported answer across the corpus |
| Conflicting constraint | Two same-day documents disagree; both sources are required |
| European pilot budget | Identical wording occurs twice; either exact Unicode range is valid |
| Battery-cabinet window | An original quote crosses a line break; synthetic analogue of `quote_not_found` |

For each case, `evidence_groups` is an AND of groups and an OR within each group. For example, the
conflict case needs evidence from both documents, while the budget case accepts either of its two
identical original occurrences. An empty list means no evidence should be cited.

The repository owner accepted the six question, answer, scope, and evidence expectations on
2026-10-04. The line-break case targets the known failure *type*; it is not a claim that the exact
observed Luna response has been reproduced.

`backend/scripts/score_m1_5_observations.py` defines the offline scorer for complete
observation set. Each observation names selected and cited **expected catalog passage IDs**, the
total `citation_count` including citations that match no expected passage, a terminal coverage or
safe-failure outcome, elapsed time, and any independently assessed statement support. Unexpected
citations lower the citation score rather than invalidating the entire observation set.
The run also records lexical, embedding, index, selector, model-pair, and prompt versions so
comparison results are attributable to a specific configuration.
The scorer checks the exact corpus digest and all six cases before reporting retrieval evidence
recall, coverage matches, citation matches, safe failures, and latency as descriptive counts. A
deterministic offline observation cannot claim real provider usage or cost; a separately opted-in
live observation must record provider-reported usage and estimated cost. The scorer does not infer
support from model output or accept raw prompts and source text in its observation schema.

New observation sets use `schema_version: 2`. Each case also records `citation_passage_ids`:
one tuple of reviewed passage IDs per statement-to-evidence link, in link order. A reused span
retains one entry for each link; an unmatched citation retains an empty tuple. The tuple count
must equal `citation_count`, and the union of all mapped IDs must equal `cited_passage_ids`.
Mappings still require selected, in-scope evidence and exact canonical source ranges.

The summary records `scoring_version: m1.5-observation-score-v2`. Citation match requires every
citation to map to an expected passage, all required evidence groups to be present, and exact
canonical citation validity. Multiple citations inside one expected passage are allowed; an
unmatched or unexpected citation fails the case's citation score. Abstention requires no citations.
Neither mapping nor scoring replaces statement-support evaluation or supplies independent review.

Version-1 observations keep `m1.5-observation-score-v1` and the original count-versus-distinct-ID
rule. Historical summaries without `scoring_version` are interpreted as version 1. Missing
per-citation information cannot be reconstructed from aggregate IDs. Do not upgrade an old report
or compare v1 and v2 citation-match counts as though the metric were unchanged; use v2 for both
sides of a future policy comparison. The corpus and expected evidence labels remain unchanged.

`backend/scripts/map_m1_5_passages.py` maps selected canonical chunks and validated original-source
citation ranges to reviewed passage IDs. It requires matching document identity, source locator,
position, and exact original text. A selected chunk may contain both repeated budget occurrences,
but a citation maps only to the occurrence at its own original-source offsets. A full-chunk citation
does not count as a precise reviewed passage citation.

`backend/scripts/score_m1_5_retrieval.py` separately scores retrieval-only observations. It checks
whether every required reviewed evidence group is available in the selected M1 context. The
unanswerable case is excluded from its evidence-availability denominator. This result does not
measure generated answers, citation selection, statement support, provider usage, or cost. Its
schema records that the embedding and dense-index adapters are deterministic test substitutes;
it cannot be mistaken for a live Pinecone/OpenAI run.

Run the credential-free retrieval baseline from `backend/` with an unused output filename:

```bash
m15_observations="../tmp/m1_5_retrieval_$(date +%Y%m%d%H%M%S).json"
INVESTMENT_RESEARCH_M1_5_RETRIEVAL_OUTPUT="$m15_observations" \
  uv run --locked python -m scripts.verify_postgres
uv run --locked python -m scripts.score_m1_5_retrieval \
  --observations "$m15_observations"
```

The existing PostgreSQL verifier creates and removes an isolated database and volume. Its M1.5
integration test parses the three approved originals, indexes their chunks with deterministic
test adapters, and runs PostgreSQL lexical search, canonical dense qualification, and the unchanged
M1 context selector on all six questions. It writes the observation file only when the output
variable is set and refuses to overwrite an existing file. The normal CI integration job runs the
same test without writing an artifact; it guards the five answerable evidence-availability cases.

The first local run on 2026-10-05 found all required reviewed passages in the selected context for
**5/5 answerable cases**; the unanswerable case has no retrieval-hit label. Its median measured
retrieval time was 6.5 ms and maximum 11 ms. These timings cover the deterministic query embedding,
PostgreSQL lexical search, SQLite vector lookup, canonical qualification, selection, and passage
mapping, but exclude parsing, indexing, answer generation, support evaluation, and network providers.
Times will vary by machine. The embedding rule explicitly aliases the known transformer paraphrase;
neither this run nor its 5/5 count estimates OpenAI or Pinecone retrieval quality.

## Paired M1/RRF offline comparison

`test_m1_5_rrf_comparison_postgresql.py` compares the two selectors without changing the approved
originals, question catalog, embedding rules, index, or context bounds. Each question runs query
embedding, PostgreSQL lexical retrieval, SQLite dense retrieval, and canonical qualification once;
both selectors receive those same immutable qualified lists. Their defaults remain eight chunks
and 24,000 Unicode code points. No answer or support model is invoked.

For this comparison only, the existing fixture helper accepts a deterministic lexical-output ID
factory. UUIDv5 IDs are derived from the corpus digest, catalog document ID, parser/chunking config,
and output position under `m1.5-paired-identities-v1`. This keeps lexical, dense, and RRF tie-breaking
repeatable across disposable databases. Normal fixtures and production ID generation are unchanged.
The test reconstructs IDs and replays both selectors to check reproducibility.

The new `m1.5-paired-retrieval-v1` report is separate from the legacy retrieval observations and v2
answer observations. It records the shared processing/provider-substitute configuration, corpus
digest, qualified-lane fingerprint, lane counts, selected chunk IDs, expected passage IDs, and each
required evidence group's first context position. Fingerprints include lane order, stable chunk
IDs, catalog document IDs, locators, original block starts, text digests, and raw scores; they exclude
temporary workspace/version UUIDs and raw text. One paired case supplies the common fingerprint,
not two independently retrieved snapshots.

The measured common phase includes query embedding, both searches, canonical qualification, and
the session boundary. Each selector is timed separately in nanoseconds. Reported retrieval time
adds that common phase to the relevant selector time, rounded down to milliseconds. Parsing,
indexing, passage mapping, generation, support evaluation, browser work, and real network providers
are excluded. Single-run timing is descriptive, not a performance benchmark.

Run from `backend/` with a fresh temporary output directory (the following workflow was verified
locally on macOS):

```bash
m15_comparison_dir="$(mktemp -d /private/tmp/investment-research-rrf-compare.XXXXXX)"
INVESTMENT_RESEARCH_M1_5_RRF_COMPARISON_OUTPUT="$m15_comparison_dir/comparison.json" \
  uv run --locked python -m scripts.verify_postgres
uv run --locked python -m scripts.compare_m1_5_retrieval \
  --observations "$m15_comparison_dir/comparison.json"
```

The verifier removes only its disposable database, networks, and volume. The content-free report
remains in the temporary directory for inspection; an existing filename is never overwritten.
Required CI runs the same paired test through the existing PostgreSQL job without producing an
artifact or using live providers.

### Recorded paired offline result (2026-10-07)

Both policies retained all required evidence for **5/5 answerable cases**. All six selected chunk
sets and their order were identical; no default-policy change is justified by this measurement.

| Case | Lexical hits | Qualified dense hits | First evidence-group ranks, M1 | First evidence-group ranks, RRF |
| --- | --- | --- | --- | --- |
| FY2025 revenue | 1 | 4 | 1 | 1 |
| FY2026 rollout constraint | 0 | 4 | 1 | 1 |
| FY2026 dividend | 0 | 12 | Not labeled as answerable | Not labeled as answerable |
| Conflicting constraint | 0 | 8 | 5, 7 | 5, 7 |
| European pilot budget | 1 | 4 | 1 | 1 |
| Battery-cabinet window | 0 | 4 | 2 | 2 |

Median retrieval time was 8 ms for both policies; maximum was 9 ms. Selector times ranged from
0.030–0.039 ms for M1 and 0.069–0.188 ms for RRF, excluding mapping and report construction.
These one-run local timings do not establish a general latency difference.

Four questions had no lexical hits, so they offer no two-lane fusion comparison. The two other
cases also produced unchanged contexts. The deterministic embedding rule still explicitly aliases
the known transformer paraphrase. This narrow sample demonstrates paired mechanics and preserves
known evidence; it does not show a quality improvement, estimate live provider retrieval quality,
measure citation precision, or close the v2 answer-comparison gate. Keep M1 as the application
default and the accepted live baseline unchanged. A separately approved, bounded provider-backed
comparison remains necessary before any RRF default decision.

## Complete offline answer baseline

The complete offline answer baseline uses the same corpus setup from
`backend/tests/integration/m1_5.py` and the actual `AnswerResearchRequestService`. It runs query
embedding, both retrieval lanes, canonical qualification, M1 selection, generation, evidence
validation, and atomic answer persistence. It then reads answers, statements, validations, and
spans back from PostgreSQL and maps the selected context and exact citations to reviewed passages.
The original parser and offline indexing setup happen before measurement. This is an application
integration test; it does not measure browser rendering or HTTP streaming.

Run it from `backend/` with an unused output filename in the existing ignored `tmp/` directory:

```bash
m15_answers="../tmp/m1_5_answers_$(date +%Y%m%d%H%M%S).json"
INVESTMENT_RESEARCH_M1_5_ANSWER_OUTPUT="$m15_answers" \
  uv run --locked python -m scripts.verify_postgres
uv run --locked python -m scripts.score_m1_5_observations \
  --observations "$m15_answers"
```

The output contains only case and passage IDs, configuration versions, terminal states, citation
counts, and measured times. It refuses to overwrite an existing file. The CI PostgreSQL job runs
the same test without writing an artifact. Two additional integration cases deliberately change
a quote or reject statement support; both must persist a `REJECTED` answer without statements or
citations.

The first complete offline run on 2026-10-05 recorded **6/6 matching coverage and citation
outcomes**, including one conflict and one abstention. All five answered cases had exact valid
citations. Its median measured application time was 47 ms and maximum 71 ms, including canonical
readback and passage mapping. These values describe this local scripted run only. The generator
uses the reviewed expected answer and resolves its quotes at the reviewed source positions inside
the actual selected context. The support adapter uses explicit fixture rules. Neither adapter
measures model quality or discovers conflicts independently; the scorer therefore records zero
independent support assessments and no provider usage or cost.

Keep offline results separate from any bounded, opt-in live model run, and do not describe this
six-case sample as a population-level quality estimate. Independent support assessment remains
pending; the separately recorded live starting measurement is described below.

## Recorded live starting measurement

The owner executed the Terra/Terra M1 baseline on 2026-10-06. The historical, ignored report is
`outputs/m1_5/m1-live-inspection-20261006-232325.json`; it is not a committed fixture or a source
of product requirements. Its reviewed corpus digest is
`ee526a2b708161e0c017b287529a62820978093e479fc78f2cea75a96a41d5f9`. The report records the unchanged
M1 round-robin selector, `m1-grounded-answer-prompt-v4`, and `m1-statement-support-prompt-v1`.
Re-scoring its observations against the reviewed catalog reproduces the following counts:

- Required evidence available in context: **5/5 answerable cases**.
- Expected coverage matched: **5/6 cases**, including the expected abstention and source conflict.
- Exact canonical citations valid: **4/4 cases with published citations**.
- Catalog citation-match score: **4/6 cases**, including the correctly uncited abstention.
- Safe operational failures: **1/6 cases**; no unsafe failure was recorded.
- Median question-phase elapsed time: **4,284 ms**; maximum: **6,073 ms**. These exclude the
  **2,754 ms** preparation phase and do not measure browser rendering.
- Retained OpenAI reservations: **USD 0.650548**, not actual charges. Report-derived total usage
  and usage-based cost estimate are **unknown**, not zero. Cleanup was recorded as **VERIFIED**.
- The owner reported approximately **USD 0.07** in the OpenAI usage view for this run. This is
  a user-reported dashboard amount, not a total reconstructed from the measurement report;
  its attribution to this invocation has not been independently verified.

The pilot-budget case failed with answer-generation contract detail `quote_ambiguous`: the same
wording occurs twice in the selected original source. The evidence was retrieved, but no accepted
answer or citation was published. This is a baseline citation-selection failure, not permission
to accept an arbitrary occurrence or a whole-chunk citation.

The rollout-constraint case completed with two exact valid citations but only one distinct
reviewed passage ID. The version-1 scorer requires total citation count to equal the number of
distinct matched passage IDs, so it marks this case's catalog citation match false. Keep that
historical metric limitation separate from canonical citation validity. The version-2 contract
above corrects it for new observations with explicit per-citation mappings; this report contains
no such mappings and retains its original 4/6 citation-match score.

This historical run used a temporary **USD 1.00** invocation ceiling. Its reservations stayed
below the subsequently approved **USD 0.80** ceiling, but it was not executed under that new
admission limit and is not a fresh verification of it. Preserve the original report unchanged.
Independent support assessments are **0/6**; the application's own evaluator is not independent
review. This historical run did not close Step 0; a comparison also needs new v2
observations on both sides rather than relabeling this historical run.
These six cases provide a descriptive starting measurement, not a general RAG-quality claim.

## Recorded version-2 baseline and review

The owner executed `outputs/m1_5/m1-live-v2-20261006-235436.json` with Terra/Terra, the unchanged
M1 selector and prompts, and the approved **USD 0.80** invocation ceiling. Its report SHA-256 is
`8a88b1f5bfa23e261025368ffc3ffaa12d010047e8e9a975e1af1c733880f469`. The reviewed corpus digest
is unchanged. Its observations use schema version 2 and `m1.5-observation-score-v2`:

- Required evidence available in context: **5/5 answerable cases**.
- Expected coverage and catalog citation match: **4/6 cases** each.
- Reported exact canonical citation validity: **3/3 cases with published citations**.
- Expected abstention: **1/1**; expected conflict outcome: **0/1**.
- Safe operational failures: **2/6**; no unsafe failure was recorded.
- Median question-phase elapsed time: **3,362.5 ms**; maximum: **7,158 ms**. Preparation took
  **3,529 ms** separately; these values exclude browser rendering.
- Retained reservations: **USD 0.606014**, not actual charges. Total provider usage and its
  cost estimate remain unknown. Cleanup was **VERIFIED**.

Do not transfer the owner's approximately USD 0.07 dashboard observation from the earlier run to
this run. Do not compare the v1 and v2 citation-match counts as a quality improvement or infer a
latency improvement from two small, non-identical outcome sets.

### Qualitative review and baseline acceptance

**Reviewed and accepted as the starting baseline:** 2026-10-07.

Codex compared the owner-supplied terminal transcript with the reviewed catalog and the two
pasted-text originals, separately from the application's support-model decisions. The corpus
verifier also confirmed every catalog source range, including the revenue passage against the
actual extracted PDF page, on 2026-10-07. The owner accepted this starting measurement after the
six-case outcomes and both retained failures were explained. This qualitative comparison and
baseline approval are not a separately executed independent-model evaluation or a replay of
persisted evidence. They add no `support_assessment` to the historical report; its count remains
**0/6**.

| Case | Content comparison | Recorded application outcome |
| --- | --- | --- |
| FY2025 revenue | Matches USD 184.2 million, 22.0% growth, and USD 151.0 million in FY2024 | Published |
| FY2026 rollout constraint | Matches the high-voltage transformer shortage | Published |
| FY2026 dividend | Abstention matches the reviewed absence of dividend evidence | Insufficient evidence |
| Conflicting constraint | Both proposed claims match the two disagreeing source reports | Rejected after the transformer statement received `supported=false` |
| European pilot budget | The proposed EUR 12 million value matches the source | Rejected because the exact quote occurs twice |
| Battery-cabinet window | The proposed Q2 2027 timing matches the original | Published |

The conflict case is a suspected support-model false rejection, not a demonstrated retrieval
failure. Its boolean decision does not reveal the model's reason. The budget case is the known
exact-substring selection limitation targeted by Step 2; neither rejected proposal counts as a
published successful answer. Preserve both failures in the baseline instead of retrying until
they disappear or weakening validation.

The attached battery quote contains `toshift`, whereas the reviewed original contains `to shift`.
The owner checked the original console output and reported no `toshift` occurrence, only
`to shift`. An offline replay of the attached proposal through the current answer adapter rejected
it with `quote_not_found`; all 41 adapter unit tests passed. This supports a transcript-transfer
discrepancy rather than a demonstrated validation defect. The precise transfer cause was not
established, and the historical live run was not replayed. The attached quote is therefore not
independent proof of exact citation validity; the canonical-validity count remains report-derived.
No fuzzy quote resolution, source-check bypass, or change to the historical report was made.

Step 0 is accepted for the starting-baseline scope. Preserve this recorded v2 run as the
comparison baseline with both failures; passing every live case is not required to preserve a
starting measurement. Unknown aggregate usage and cost remain unknown, and canonical citation
validity remains report-derived. Acceptance does not close either failure or the final M1.5
quality gate. RRF had not started at baseline acceptance; its subsequent offline comparison is
recorded above. Broader model-quality claims require later evaluation.

## Credential-free live preflight

From `backend/`, print the approved plan without loading settings, reading keys, connecting to a
database, or contacting a provider:

```bash
uv run --locked python -m scripts.plan_m1_5_live_baseline
```

The command pins the reviewed catalog digest, validates the original fixtures, and reports only
case IDs, configuration versions, model choices, prices, and limits. The default model pair is
Terra/Terra. `--answer-model gpt-5.6-luna` and `--support-model gpt-5.6-luna` independently select
the existing Luna adapter policy. `--max-spend-usd 0.20` lowers the per-invocation OpenAI estimate
ceiling; its default and maximum are USD 0.80. This is separate from the application's USD 5
monthly budget and does not authorize paid execution.

The reusable `LiveBaselineBudget` reserves each attempted call **before** provider access: UTF-8
payload bytes plus the existing answer/support envelope, the adapter's full output-token limit,
and the selected model's versioned repository price. Integer micro-dollar rounding is upward.
Reported usage must fit the reservation; an overrun stops subsequent admission. Reservations are
never refunded, including a timeout with unknown usage, and retries would consume another request
and reservation. This guard is for a sequential evaluation runner, not concurrent application
requests or a replacement for the canonical production accounting service.

Independent request ceilings are 64 document-embedding calls, six query-embedding calls, six
answer-generation calls, and 48 support-evaluation calls (eight statements per case). The document
vector ceiling is also 64; the future runner must enforce it when admitting actual chunks. These
are safety ceilings, not predicted call counts. The USD 0.80 accounting ceiling can stop the run
before every request ceiling is reached; completion is not guaranteed by preflight. Budget-limited
cases must be recorded as failures rather than silently dropped from quality statistics.

The price basis is the repository's versioned estimate, not a provider invoice or a guarantee
against future price changes. Input envelopes are conservative estimates rather than exact API
token counts. Output limits include hidden reasoning and formatting tokens, as described in
[OpenAI's token-counting guide](https://developers.openai.com/api/docs/guides/token-counting).
The OpenAI estimate excludes Pinecone billing; any later live runner must require the existing
Starter-only confirmation and an isolated namespace with verified cleanup.

The preflight and budget tests are credential-free and run in the existing unit-test CI job.
They do not call providers or change the website.

## Live-runner safeguards

`backend/scripts/m1_5_live_providers.py` wraps the existing embedding, answer, and support ports
with the invocation budget. It reserves before every attempted call and records only its role,
reported token usage, estimated cost, and reservation. A failed call with unavailable usage is
`UNKNOWN`, never zero-cost. Reported usage does not imply a correct or accepted answer. Canonical
application accounting and evidence validation remain separate and unchanged.

`backend/scripts/m1_5_live_index.py` owns a fresh random workspace namespace for each invocation.
It rejects foreign scopes and admits at most 64 unique vector IDs. Attempted IDs are tracked
before writes, because a timeout does not prove a write failed. Visibility polling uses one
30-second deadline across batches, bounds each fetch by its remaining time, and rejects late
success. After attempted writes, cleanup deletes only the owned namespace and verifies that all
attempted IDs are absent within a separate 30-second deadline. Both index clients are closed even
when cleanup fails; failed cleanup is reported rather than treated as success.

Deterministic unit tests exercise these safeguards with injected adapters and a simulated clock;
no live clients or credentials are used. PostgreSQL integration tests exercise the complete runner
with scripted providers, including budget rejection, provider failure, and changed quotes. These
are correctness tests, not live quality measurements.

## Opt-in live baseline

From `backend/`, preview the same admission plan without loading runtime settings or keys:

```bash
uv run --locked python -m scripts.verify_m1_5_live_baseline
```

Actual execution requires Docker, the existing complete M1 provider configuration,
and explicit approval to send the frozen public-synthetic corpus to the configured providers.
The evaluation-only loader reads the root local configuration at runtime; agents must not inspect
environment or secret files. Existing
roles are document embeddings, query embeddings, Pinecone indexing/cleanup, Pinecone querying, and
the shared answer/support credential. No new credential or provider is introduced. The Pinecone
index must already exist with the accepted M1 configuration and Starter confirmation; this tool
does not provision an index.

The five existing `INVESTMENT_RESEARCH_*_API_KEY_SECRET_FILE` settings are supported without
duplicating keys as direct values. Relative paths resolve from the repository root and must name
regular, bounded, UTF-8 files directly under its real `.secrets/` directory. Absolute paths to that
same directory are allowed; symlinks, traversal, non-regular files, and conflicting direct/file
values are rejected. Direct `INVESTMENT_RESEARCH_*_API_KEY` values remain an alternative, not an
additional requirement. Process variables override their corresponding local-file variables;
credentials are never exported into the process environment. Mounted Docker `_FILE` settings stay
exclusive to the normal application loader.

The Pinecone host and `INVESTMENT_RESEARCH_PROVIDER_MONTHLY_BUDGET_MICRO_USD` remain required.
The latter must satisfy the existing USD 5 monthly application ceiling; the invocation guard is
still independently capped at USD 0.80. Ordinary PostgreSQL, Redis, S3, parser, runtime-location,
and raw-output settings are not loaded: the tool owns its evaluation database and does not execute
the worker delivery path. Credential/model validation reuses the existing application settings.
Configuration errors name only a fixed setting and reason, never a value, secret path, or raw
validation exception. Free preflight does not invoke this loader.

The following command enables paid calls and creates derived vectors. Live measurements are
separate from deterministic verification and do not imply quality acceptance. Use a new report
filename each time:

```bash
uv run --locked python -m scripts.verify_m1_5_live_baseline \
  --execute \
  --confirm-public-synthetic \
  --confirm-pinecone-starter \
  --output ../outputs/m1_5/m1-live-baseline.json
```

Terra/Terra remains the default. Add `--answer-model gpt-5.6-luna` and/or
`--support-model gpt-5.6-luna` for an explicitly selected lower-cost model pair; no automatic
fallback occurs. The USD 0.80 reservation ceiling may prevent all Terra cases from completing.
Record this as budget-limited execution, not evidence that retrieval quality is poor, and do not
increase the approved ceiling implicitly.

The tool owns a random disposable PostgreSQL project and loopback port, migrates it through
Alembic, loads only the approved corpus, and uses a fresh Pinecone workspace namespace. It reuses
the evaluation fixture builders and unchanged M1 retrieval, generation, canonical accounting,
validation, and answer-persistence services. This is a development evaluation tool, not a worker
delivery/recovery test or a replacement for normal document-ingestion accounting. It never accepts
a production database URL or caller-selected namespace. By default it installs no content-output
callback; it uses no exporter or browser. Cleanup closes clients and removes only invocation-owned
resources.

### Live selector experiment

Both `plan_m1_5_live_baseline` and `verify_m1_5_live_baseline` accept
`--retrieval-policy m1|rrf`. Omitting it preserves M1. These free RRF preflight commands were
verified on 2026-10-07, from `backend/`:

```bash
uv run --locked python -m scripts.plan_m1_5_live_baseline --retrieval-policy rrf
uv run --locked python -m scripts.verify_m1_5_live_baseline --retrieval-policy rrf
```

The option changes only the frozen selector: `m1-lexical-first-round-robin-v1` or
`m1-5-rrf-k60-v1`. Corpus, prompts, models, embedding/index configuration, context limits,
role ceilings, and the maximum USD 0.80 invocation reservation remain unchanged. Runtime
composition resolves the recorded version rather than accepting arbitrary profiles or falling
back silently. PostgreSQL tests verify actual retrieval versions and RRF fusion ranks, including
budget exhaustion, provider failure, and evidence rejection. These tests use scripted adapters
and spend no provider credits. Existing CI discovers them automatically.

For a separately approved live invocation, the same option can be added to the existing explicit
execution command above. Each invocation retains its own USD 0.80 ceiling; two invocations could
therefore reserve up to USD 1.60 combined. No combined spend is implicitly approved. Always use a
new report filename and the same model pair for both policies. The application default remains
M1; this option is evaluation-only and is not a website setting.

The admission plan, v2 observations, summary, and completion message name the selected version.
Preserve the accepted historical baseline unchanged. The new option alone does not produce a
live quality measurement or independent support review. Separate live runs have separate query
calls, namespaces, random canonical IDs, and stochastic model answers; they are not a paired
candidate-snapshot experiment and a score difference cannot be attributed to fusion alone.
The paired offline comparison remains the controlled selector-mechanics check. Record these
limitations in the eventual live comparison rather than claiming a quality improvement from
this wiring change. No live RRF run was performed during selector preparation; the subsequent
owner-executed comparison is recorded below.

### Live comparison protocol

**Protocol:** `m1.5-sequential-live-comparison-v1`, proposed and approved by the owner on
2026-10-07 for exactly two invocations under the USD 1.60 combined OpenAI reservation envelope.
Both were owner-executed and are recorded below. This approval does not authorize additional
invocations, providers, budget increases, or a default-policy change.

1. Use one unchanged checkout for both runs. Freeze the reviewed corpus digest, processing and
   embedding/index configuration, answer/support prompts, Terra/Terra model pair, existing lane
   and context limits, and per-role request limits. Do not tune between runs.
2. Run all six cases once with explicit `--retrieval-policy m1`, then once with
   `--retrieval-policy rrf`. Use explicit Terra models, `--max-spend-usd 0.80`, the existing
   execution confirmations, and distinct new report filenames under `outputs/m1_5/`. The approved
   combined OpenAI reservation envelope is USD 1.60: two sequential invocations of at most
   USD 0.80 each. It is not a new application monthly limit or a price guarantee; Pinecone billing
   remains outside that estimate and requires the existing Starter-only confirmation.
3. Preserve every recorded case and failure. Do not rerun failed cases or select a better run.
   If setup prevents a report, record the interruption rather than declaring a successful
   comparison. Do not launch RRF after failed or unverified M1 cleanup. Any additional invocation
   requires new approval; there is no automatic retry or budget increase.
4. Before comparing, verify both admission plans and v2 observation configurations agree on all
   fields except the selector version, and both contain exactly the six frozen case IDs. Check
   that each report stayed within its invocation envelope and cleanup is `VERIFIED`. Retain the
   existing historical starting baseline as context, not as a substitute for the fresh M1 arm.
5. Compare required evidence availability, coverage and citation matches, abstention, conflict,
   safe/unsafe failures and their stage, question-phase latency, preparation latency, reservations,
   and reported usage/cost. Keep preparation and question accounting separate. Unknown usage or
   cost stays unknown. Application support checks are not independent review; inspect both arms
   consistently before making any answer-support quality claim.

This is a small sequential end-to-end observation, not a randomized trial or identical-snapshot
comparison. Separate namespaces, random IDs, provider retrieval, model sampling, and fixed run
order can affect results. Use the paired offline result to check selector mechanics; do not infer
that a live answer difference proves RRF caused it. Six cases cannot establish general quality or
latency. The repeated-budget-quote and conflict-support failures stay visible even if they recur.

Critical evidence/isolation violations or unverified cleanup block acceptance. Better counts
alone do not justify switching defaults. If the result is unchanged, worse, or inconclusive,
retain M1 and record that outcome rather than tuning the corpus or extending this experiment.
Any default change needs a reviewed decision and the full Step 1 verification gate.

### Recorded sequential live comparison (2026-10-07)

The owner ran the two approved invocations without selective retries:

| Policy | Ignored report | SHA-256 |
| --- | --- | --- |
| M1 | `outputs/m1_5/m1-comparison-20261007-011254.json` | `4946a8bf7533149f249f388d94f2da79230b490e8a8d9313a4caa74bf544547f` |
| RRF | `outputs/m1_5/rrf-comparison-20261007-011359.json` | `35fcffb66bda3b8bbf0f3847d020661f5e9ec29e35f70b44c1a304d1eddf314b` |

Both reports use v2 observations and `m1.5-observation-score-v2`. Corpus digests, six case IDs,
and configuration fields agree except for the intended selector version. The models remain
Terra/Terra. Recomputing both summaries with the existing scorer reproduced every recorded
summary field. This is report validation, not an independent replay of the deleted database or
an independent assessment of answer support. Historical reports were not modified.

| Recorded measurement | M1 | RRF |
| --- | --- | --- |
| Required evidence available in context | 5/5 answerable cases | 5/5 answerable cases |
| Expected coverage match | 5/6 | 3/6 |
| Catalog citation match | 4/6 | 3/6 |
| Reported exact canonical citation validity | 4/4 checked cases | 2/2 checked cases |
| Abstention match | 1/1 | 1/1 |
| Conflict match | 1/1 | 0/1 |
| Safe failures / unsafe failures | 1 / 0 | 3 / 0 |
| Independent support assessments | 0/6 | 0/6 |
| Median / maximum question-phase time | 4,062 / 7,272 ms | 3,661 / 5,095 ms |
| Preparation time | 3,474 ms | 1,946 ms |
| Retained OpenAI reservations | USD 0.650674 | USD 0.650542 |
| Cleanup | VERIFIED | VERIFIED |

Combined reservations were USD 1.301216, within the approved USD 1.60 envelope and both individual
USD 0.80 caps. Total usage and actual cost estimates remain unknown in both reports; reservations
are not charges. Shorter RRF timings do not establish better performance: different outcomes and
statement counts can change work, and each policy was observed only once.

The pilot-budget quote was ambiguous in both runs. RRF additionally rejected the rollout-constraint
and conflicting-report answers under `STATEMENT_SUPPORT` and `COVERAGE`. The transcript shows
generated claims and quotations for those cases, but the application support evaluator returned
false for at least one statement. These are post-retrieval failures, not missing required context.
The reports alone do not establish whether rejection was justified or whether changing order,
provider behavior, or model sampling caused it. Do not label them confirmed RRF defects or bypass
support validation. M1's rollout case also failed the catalog citation-match metric despite
reported exact canonical validity; the two metrics test different properties.

**Recorded decision:** Retain M1 as the application default. The paired offline experiment showed
identical contexts; the sequential live observation demonstrated no benefit sufficient to justify
adopting RRF. Preserve RRF as an explicit evaluation profile, both live reports, and all failures.
No further paid invocation is approved. The evidence-unit work targets the known ambiguous/copying
quote limitation; support-evaluator quality remains a separate evaluation concern. This decision
is not a general claim that M1 is superior or that the M1.5 quality gate has passed.

Frontend compatibility checks passed formatting, lint, typing, contract validation, and 45 tests
on 2026-10-07. Backend and PostgreSQL results for the implemented profiles are recorded in the
implementation plan. The production build and final hosted Step 1 checks remain pending; no
browser or new provider call was made while reviewing the reports.

### Interactive local inspection

Add `--show-local-model-output` to an explicitly confirmed `--execute` command to inspect the
fixed public-synthetic corpus in your terminal. This option requires interactive standard output
and rejects the presence of `CI` or `GITHUB_ACTIONS` before configuration or provider access,
including when their value is `false`. Redirecting or piping the output is not supported. The
terminal boundary is checked again at callback setup and before each content emission.

The display prints each question, the complete schema-parsed model proposal including proposed
quotes before exact-quote resolution, individual statement-support decisions, the canonically
persisted result, and the case outcome. Each support decision includes its statement and
`evidence_texts`: the exact canonical text tuple passed to the support provider, in the same order,
without trimming or normalization. This applies to both supported and rejected statements under
either answer policy. With this flag, the support provider also returns a required short English
`reason` (1–500 characters) alongside `supported`, displayed in the same event. The explanation is
untrusted model text: it neither proves the cause of a decision nor overrides a rejection.
The application carries it only as an excluded, non-repr diagnostic field; it is not stored in
canonical validation records, the measurement JSON, normal logs, traces, or website responses.
Deterministic test adapters without a reason display `null`.

This changes the diagnostic support prompt and schema, not only the display. Reports record
`m1-5-local-support-reason-prompt-v1`; ordinary runs retain `m1-statement-support-prompt-v1` and the
Boolean-only contract. Asking for a reason can change the model's decision, so do not treat these
diagnostic runs as an unchanged baseline or as an explanation of an earlier response. It uses the
same support request, timeout, output-token ceiling, reservation policy, and invocation budget;
extra explanation tokens may increase reported usage, but no follow-up call or retry is added.

The display does not include the question or other context as part of the support
input. A provider failure before a decision returns produces no support-decision event; existing
safe failure diagnostics still apply. Historical reports do not retain these texts and cannot
reconstruct an earlier failed support input. If SDK schema parsing fails, it prints only the available
generated `output_text`, never HTTP headers, credentials, or complete SDK response metadata. A
provider failure before any generated text cannot display an answer. Malformed support output
uses the existing diagnostic callback. All proposals are labeled **Unvalidated model output**;
local inspection does not bypass any evidence, usage, or publication check and does not supply an
independent support assessment.

No additional provider request is made for display. The JSON measurement report remains
content-free, and normal logging is unchanged. Do not add this flag to GitHub Actions or capture
this terminal content in shared logs. Model-authored control characters are JSON-escaped.

The JSON report contains the admission plan, all six case observations, their descriptive score
summary, safe failure categories, preparation latency, per-call usage, retained reservations, and
cleanup status. Preparation usage and total usage include document embeddings; the score summary
counts question-phase usage only. Unavailable usage and incomplete totals are `null`, never a
fabricated zero. Reported usage beyond a reservation remains recorded but stops future admission.
Only IDs, configuration, counts, and measurements are written, not questions, source text,
model proposals, answers, credentials, or provider hosts. Reports must be new `.json` files under
ignored `outputs/m1_5/`; existing files and escaped/symlinked targets are rejected.

New reports retain optional, content-free diagnostics. Each failed case records its `stage`:
preparation, index visibility, retrieval, passage mapping, generation/validation, persisted
readback, evidence validation, or citation mapping. A provider failure additionally records the
attempted role when known, a fixed category (`configuration`, `contract`, `output_limit`,
`refusal`, `transient`, `unavailable`, `usage_limit`, `unexpected`, or `interrupted`), and only
adapter-owned HTTP status or allowlisted answer-contract details when available. For example,
`quote_not_found` and `quote_ambiguous` remain distinguishable without storing the quote. The
diagnostic is also attached to the failed provider-call usage record.

Rejected answers retain only deduplicated `failed_validation_rules`, such as
`EXACT_SOURCE_SPAN` or `STATEMENT_SUPPORT`; raw validation reasons are excluded because they can
contain model-authored text. The terminal prints the same fixed diagnostics per failed case.
These optional fields preserve schema-version-1 report loading. Absent fields in older reports
mean unknown diagnostics, not successful validation or a retrospectively established cause.
Neither error messages, exception causes, raw SDK bodies, nor provider output are copied into the
measurement report. The separately enabled local terminal inspection is described above.

An operational case failure or failed cleanup produces exit code 1 while retaining a completed
measurement report. Configuration/startup failures or an interruption may leave only an empty
reserved file; it is not an accepted result. Use a new filename for a retry; do not overwrite the
earlier file. Output errors explicitly remind the caller of this requirement. A verified run can
still have weak coverage or citation
scores: exit code 0 proves execution, not the M1.5 quality gate. Independent support assessment
remains pending and is never inferred from the application's own support evaluator. The
[historical live starting measurement](#recorded-live-starting-measurement) did not close Step 0.
The owner subsequently accepted the [v2 starting baseline](#recorded-version-2-baseline-and-review)
with its documented limitations. Future comparisons must use the versioned v2 contract described
above on both sides; final quality acceptance still requires independent support assessment.
