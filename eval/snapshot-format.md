# Offline selection snapshot, version 1

The machine-readable contract is defined in
[`portfolio_snapshots.py`](../backend/scripts/portfolio_snapshots.py). Snapshots are local,
ignored JSON files under `outputs/`. The scorer reads at most 50 MiB and never connects to an
application, database, or model provider. It scores captured selections; it does not rerun a
selector, expand neighbors, rerank candidates, or fit a new generation input.

## Required top-level fields

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1`. |
| `catalog_sha256` | SHA-256 of the exact reviewed catalog bytes, including original questions. |
| `code_sha256` | Recorded producer-code fingerprint, not a claim that current code produced the selection. |
| `recorded_versions` | Version-name to version-identifier map; retain the producer's known versions. |
| `settings` | Explicit recorded scope, retrieval switches, reranker, model pair, and fully resolved RAG limits. |
| `sources` | Frozen source aliases, document/version UUIDs, optional titles, and original hashes where recorded. |
| `selections` | Ordered passages per case, discovery mode, and repetition. |

Settings must match the requested profile after inheritance from `config/rag.toml`.
Changing `text_neighbors` in a profile does not recompute a recorded context. Use a separate
compatible recording for that variant. The scorer rejects mismatched settings rather than
mislabeling the old selection. Complete saved candidate pools and selector replay are separate work.

Each source has a unique `alias`, `document_id`, and `document_version_id`. A catalog alias must
have the catalog's exact source title. Unknown additional archive sources can retain a null
title/hash when those values were not captured; do not invent them. The report records source
count and original-hash coverage. Its corpus digest covers recorded document/version/hash
identities; it is not a live canonical-eligibility or complete source-content verification.

## Selection fields

| Field | Meaning |
| --- | --- |
| `case` | Existing catalog case ID. |
| `question` | Exact original question; no retrospective query improvement. |
| `repetition` | Integer 1..10; defaults to 1. Missing repeats are not synthesized. |
| `archive_mode` | `keyword`, `semantic`, or `hybrid` for discovery; null for research. |
| `stage` | `archive_results`, `selection_before_generation_preflight`, or `final_generation_context`. |
| `candidate_pool_sha256` | Optional hash captured from the complete original candidate pool. |
| `selected` | Ordered list of `chunk_id`, source alias in `source`, physical PDF `page` or null, and unchanged `text`. |

Research selections contain at most eight chunks and respect their recorded chunk/character
limits. Discovery permits twenty previews. Every passage must reference a known source. Duplicate
selection keys, duplicate chunks in a selection, conflicting content under one chunk ID, and
unknown cases fail validation. Explicit document scope is checked against the selected case's
aliases. Workspace, deletion, active-version, and temporal authorization are not rechecked offline;
these files must never authorize product answers or citations.

## Scoring and comparison

Research marker ranks reuse the existing source/page/normalized-text matcher. Each target's
markers must all occur in the same selected passage. Only answer cases contribute to the
all-markers summary; abstention markers remain context diagnostics. Marker coverage is not a
verdict on a generated answer. Discovery expectations currently use prose, so the report lists
their selections for manual review without inventing automatic pass/fail grades.

Comparisons require the same catalog, recorded corpus identities, selected case/mode/repetition
keys, research scope, and per-case pipeline stage. They show gained/lost markers, rank changes,
and added/removed passage fingerprints. A shared immutable chunk cannot have different content.
`same_candidate_pool` compares recorded pool hashes when both exist; null means the control was
not captured. Neither equality nor a changed selection alone establishes causal improvement.

Reports retain recorded versions and input hashes but exclude full questions, passages, and raw
runtime IDs. They are written only to new `.json` files under `outputs/`, using exclusive creation.
Existing recordings and reports are never overwritten. Review and sanitize any later public export.

Legacy bare JSONL pools and HTTP stream captures need an explicit provenance-preserving conversion.
Do not infer absent switches, fabricate corpus fingerprints, silently drop incomplete results,
or assign today's configuration to an older recording. The existing frozen JSONL scorer remains
available for its original historical format.
