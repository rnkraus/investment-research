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
six-case sample as a population-level quality estimate. Independent support assessment and live
model baseline measurements are still pending.

## Credential-free live preflight

From `backend/`, print the approved plan without loading settings, reading keys, connecting to a
database, or contacting a provider:

```bash
uv run --locked python -m scripts.plan_m1_5_live_baseline
```

The command pins the reviewed catalog digest, validates the original fixtures, and reports only
case IDs, configuration versions, model choices, prices, and limits. The default model pair is
Terra/Terra. `--answer-model gpt-5.6-luna` and `--support-model gpt-5.6-luna` independently select
the existing Luna adapter policy. `--max-spend-usd 0.50` lowers the per-invocation OpenAI estimate
ceiling; it cannot exceed USD 1. This is separate from the application's USD 5 monthly budget and
does not authorize paid execution.

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
are safety ceilings, not predicted call counts. The USD 1 accounting ceiling can stop the run
before every request ceiling is reached; completion is not guaranteed by preflight. Budget-limited
cases must be recorded as failures rather than silently dropped from quality statistics.

The price basis is the repository's versioned estimate, not a provider invoice or a guarantee
against future price changes. Input envelopes are conservative estimates rather than exact API
token counts. Output limits include hidden reasoning and formatting tokens, as described in
[OpenAI's token-counting guide](https://developers.openai.com/api/docs/guides/token-counting).
The OpenAI estimate excludes Pinecone billing; any later live runner must require the existing
Starter-only confirmation and an isolated namespace with verified cleanup.

The preflight and budget tests are credential-free and run in the existing unit-test CI job.
They do not call providers or change the website. The paid live runner, actual usage recording,
independent support review, and live quality results are **not implemented by this preflight**.
