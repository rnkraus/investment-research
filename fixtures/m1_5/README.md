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
reviewed passage ID. The current scorer requires total citation count to equal the number of
distinct matched passage IDs, so it marks this case's catalog citation match false. Keep that
metric limitation separate from canonical citation validity. Do not reinterpret historical
scores or modify labels silently; review and version any scoring change before a comparison.

This historical run used a temporary **USD 1.00** invocation ceiling. Its reservations stayed
below the subsequently approved **USD 0.80** ceiling, but it was not executed under that new
admission limit and is not a fresh verification of it. Preserve the original report unchanged.
Independent support assessments are **0/6**; the application's own evaluator is not independent
review. Step 0 remains open pending that review and resolution of the comparison-metric contract.
These six cases provide a descriptive starting measurement, not a general RAG-quality claim.

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

### Interactive local inspection

Add `--show-local-model-output` to an explicitly confirmed `--execute` command to inspect the
fixed public-synthetic corpus in your terminal. This option requires interactive standard output
and rejects the presence of `CI` or `GITHUB_ACTIONS` before configuration or provider access,
including when their value is `false`. Redirecting or piping the output is not supported. The
terminal boundary is checked again at callback setup and before each content emission.

The display prints each question, the complete schema-parsed model proposal including proposed
quotes before exact-quote resolution, individual statement-support decisions, the canonically
persisted result, and the case outcome. If SDK schema parsing fails, it prints only the available
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
[recorded live starting measurement](#recorded-live-starting-measurement) does not close Step 0;
independent review and the comparison-metric decision must also be recorded.
