# Evaluation

## Evaluation-profile draft

[The commented profile template](profiles/standard.example.toml) proposes per-experiment
settings with descriptions, planned values, and offline/live constraints beside every field.
It is a design example: no current runner reads it, and editing it changes neither the website
nor an existing evaluation command. Profile loading, strict validation, and a credential-free
run preview are the next implementation block. Existing verified commands below remain valid.

## Recorded evaluations

- [Observation-date follow-up](observation-date-followup.md) records the targeted U03
  correction, unchanged eight-chunk context, successful abstention, and its verification limits.
- [Current RAG and archive-search evaluation](rag-archive-current-run.md) records the repeat
  of all 19 cases with Terra/Terra and additional hybrid discovery requests: nine complete
  answer cases, two of three expected abstentions, and the remaining date/search gaps.
- [First app run on public sources](first-app-run.md) covers archive search, cited answers, and
  abstention across 19 reviewed cases. It includes expected results, observed gaps, and timings.
- [Retrieval and reranking follow-up](retrieval-and-reranking.md) records the search
  experiments, the fixed-pool Luna comparison, and separate runtime checks.
- [Neighbor context app evaluation](neighbor-context-app-run.md) records the 19-case
  whole-archive run with bounded neighboring text and Terra/Terra, including remaining
  discovery and abstention-message gaps.
- [Individual and batched evidence checks](../docs/13-m1-5-rag-quality-plan.md#batched-statement-support-in-the-local-api)
  compares judgments, calls, latency, token use, and estimated cost on seven fixed synthetic
  statements using Luna.
- [M1.5 synthetic evaluation](../docs/14-m1-5-portfolio.md#3-recorded-evaluation-not-a-general-accuracy-claim)
  records the earlier source-unit and citation checks on a different corpus and model configuration.

## Portfolio baseline catalog

[Catalog v1](portfolio-cases-v1.json) records the original 19 questions, discovery filters,
document scopes, and expected outcomes. The nine answer cases specify evidence markers by source
and page. The three abstention cases include contextual landmarks for manual review. The whole-archive
replay used a more specific A06 query, which is recorded separately from the original question.

From `backend/`, score a local saved whole-archive retrieval export:

```shell
uv run --locked python -m scripts.score_portfolio_retrieval /path/to/saved-pools.jsonl
```

The command reads a JSONL export with all twelve research cases. Each row contains `case`,
`question`, `selector`, ordered selected IDs in `app`, and `candidates` with `id`, `title`,
`page` (or null for pasted text), and `text`. Keep saved candidate pools and passage text
local. The command makes zero database or provider calls and prints only evidence-marker ranks,
case IDs, selector versions, and input digests. A match requires the expected source title and
page and every marker in the same selected passage, with case and whitespace normalization.

The saved export describes selection **before generation preflight**. The eight-passage selection
can shrink when the app prepares the actual model input. Marker hits are a diagnostic for this
recorded extraction. Review alternate passages manually if their wording differs. The seven
discovery cases use their own search expectations. Grade abstention, answer completeness, and
actual citation support using an end-to-end run.

## Four-case app pilot

The pilot uses A05 (Citi financing), A06 (Inferize), A09 (operating ARR), and U03
(unreported November 6 ARR). It uses the original catalog questions and selected-document
scopes. The prepared bodies select Terra for both model roles and standard search. Start the
local API with the source-ID profile so it uses batched evidence checks. Record the code
revision and active RAG configuration with the results.

Create an ignored local JSON map from source aliases to the verified current
`document_id` and `document_version_id` of each reviewed original. Include `N-CITI`,
`N-INF`, `I-Q1`, `I-NEW`, and `X-CHANOS`. From `backend/`, prepare the request bodies
and preview the maximum number of provider attempts:

```shell
uv run --locked python -m scripts.inspect_portfolio_pilot /path/to/sources.json
```

Pass `--case A09` to print only that case's request body for the normal
`POST /api/v1/research-requests` endpoint. After checking the budget, submit that body once
to the local API and retain the unmodified SSE response. In the browser, the request and the
stream are visible in the Network panel. Inspect the saved response locally:

```shell
uv run --locked python -m scripts.inspect_portfolio_pilot /path/to/sources.json \
  --case A09 --stream /path/to/A09.sse
```

The inspector scores `research.context` after the application fits the input. It verifies
the document and version of each selected passage, reports marker ranks and terminal status,
and counts linked statements and citations. Open each citation and review the claim against
its original. Record citation validity, factual completeness, and provider usage separately.
The pilot requires correct source links, support for every published claim, and an
insufficient-evidence result for U03. Grade completeness separately.

Both commands are offline. The attempt preview assumes one submission per case and includes
the configured transient retrieval retry. The API applies its workspace budget to actual
provider reservations. Review available budget before submitting the pilot and record the usage
ledger afterward. Keep raw streams, local IDs, and unreviewed results out of Git.

## Weighted OR lexical retrieval experiment

The PostgreSQL adapter supports an opt-in `weighted_or` strategy for plain queries. A chunk
can match any query lexeme. Its score sums the inverse document frequency of each distinct
matched lexeme, where the corpus consists of the eligible chunks in the query's scope. The
formula is `ln(1 + (N - df + 0.5) / (df + 0.5))`, with `N` eligible chunks and `df` eligible
chunks containing the lexeme. Queries using explicit phrases, `OR`, or exclusions, and
queries outside the one-to-16-lexeme range, use the existing `websearch` strategy. The app
continues to use `websearch` by default.

The 2026-11-17 pilot replay used the original document scopes, the same saved Pinecone hits,
and the existing RRF and context expansion. The comparison measures ranks in the eight
selected passages before generation preflight. It made no provider calls.

| Case | `websearch` | `weighted_or` |
| --- | --- | --- |
| A05 · Citi financing | Size/lender at 1, maturity/spread at 5 | Size/lender at 1, maturity/spread at 3 |
| A06 · Inferize | Platform at 1 | Platform at 1, cold-start bottleneck at 7. GPU-idle mechanism appears at lexical rank 9 and stays outside the final selection. |
| A09 · Operating ARR | All three markers at 2 | All three markers at 3 |
| U03 · Missing November 6 ARR | September-only evidence at 7 | September-only evidence falls outside the selection. Neither variant showed evidence for November 6 in the reviewed scope. |

In a separate lexical-adapter control for D07, both strategies returned five stem matches in
the IREN scope and zero exact `Inferize` matches. The experiment measured retrieval only.
Archive-wide latency and answer quality remain open checks before making this an app option.
