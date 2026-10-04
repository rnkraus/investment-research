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
observed Luna response has been reproduced. The next work block measures the current M1 retrieval
and answer path on these fixed cases. Keep deterministic offline results separate from any bounded,
opt-in live model run, and do not describe this small sample as a population-level quality estimate.
