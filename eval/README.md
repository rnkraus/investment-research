# Evaluation

- [First app run on public sources](first-app-run.md) covers archive search, cited answers, and
  abstention across 19 reviewed cases. It includes expected results, observed gaps, and timings.
- [Retrieval and reranking follow-up](retrieval-and-reranking.md) records the search
  experiments, the fixed-pool Luna comparison, and separate runtime checks.
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
