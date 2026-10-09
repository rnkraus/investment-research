# M1.5 closeout question review

**Status:** Version 1 was owner reviewed and accepted on 2026-10-09. The separate version-2
budget-question correction below awaits owner review. Neither status is live answer-quality or
M1.5 acceptance.

The machine-readable source of truth is
[`evaluation-closeout-v1.json`](./evaluation-closeout-v1.json), catalog ID
`m1.5-rag-quality-closeout-v1`. It reuses the same three fictional CC0 originals and preserves
all six cases and six targets from the reviewed [`evaluation.json`](./evaluation.json).
One additional exact target covers the supplier lead-time expectation in the capacity update.
The historical six-case reports and their scores remain unchanged.

## Version-2 budget-question correction — pending review

[`evaluation-closeout-v2.json`](./evaluation-closeout-v2.json), catalog ID
`m1.5-rag-quality-closeout-v2`, preserves all originals, seven source targets, scopes, expected
answers, coverage labels, and the first eleven cases from version 1. Only the final question and
its ID change, besides the catalog ID and pending review status:

| Field | Frozen version 1 | Proposed version 2 |
| --- | --- | --- |
| Question ID | `question:fy2025-revenue-and-fy2026-pilot-budget` | `question:fy2025-revenue-and-european-pilot-budget` |
| Question | What were Northstar's FY2025 revenue and year-over-year growth, and what is its FY2026 European pilot budget? | What were Northstar's FY2025 revenue and year-over-year growth, and what is its European pilot budget? |
| Expected coverage | COMPLETE | COMPLETE |
| Required evidence | Revenue/growth sentence and either original budget occurrence | Unchanged |

**Reason:** The operations note states the budget amount, not its fiscal applicability. Its
publication date and fixture filename do not establish an FY2026 budget period. A model that
answers version 1 with PARTIAL and declines to assign that period is not necessarily wrong.
Version 2 removes the unsupported premise while retaining a fully answerable two-document probe;
it does not retrospectively turn any version-1 PARTIAL response into a version-2 success.

The owner must review this exact wording and unchanged evidence expectations before activation.
The live runner still maps `--corpus closeout` to reviewed version 1; version 2 is not admitted
for paid runs. Do not overwrite historical reports, recompute their recorded scores, or compare
the changed question as an unchanged paired experiment. These are developer-informed probes,
not held-out quality evidence.

Check the pending draft's schema and original-source integrity from `backend/`:

```bash
uv run --locked python -m scripts.verify_m1_5_corpus \
  --manifest ../fixtures/m1_5/evaluation-closeout-v2.json
```

Adding `--require-reviewed` must currently fail. Counts remain twelve cases: seven COMPLETE,
two PARTIAL, one CONFLICTING, and two INSUFFICIENT. Scoring, runtime validation, prompts,
budgets, and the frozen version-1 expectations below are unchanged.

## Documents and scope

| Short name | Approved original |
| --- | --- |
| Earnings brief | [FY2025 PDF](../m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf) |
| Capacity update | [FY2026 text](../m1/documents/northstar-grid-analytics-fy2026-capacity-update.txt) |
| Operations note | [FY2026 text](./documents/northstar-grid-analytics-fy2026-operations-note.txt) |

Only the documents in a case's `scope_document_ids` can supply its answer. A fact found elsewhere
in the archive does not make a scoped question answerable. File hashes and exact Unicode source
ranges are verified offline; their semantic appropriateness still needs human review.

## Questions and expected behavior

Question suffixes below identify the `question:` IDs in the catalog. The JSON contains the exact
English question wording, reference answer, and required source ranges.

| Question suffix | Scope | Expected coverage | Expected content / review rule |
| --- | --- | --- | --- |
| `fy2025-revenue` | Earnings brief | COMPLETE | FY2025 revenue USD 184.2 million; up 22.0% from USD 151.0 million. Cite the revenue sentence, not its PDF heading. |
| `fy2026-rollout-constraint` | Capacity update | COMPLETE | High-voltage transformer shortage. Keep the frozen target; extra lead-time citations still fail this case's strict citation metric. |
| `fy2026-dividend` | All three | INSUFFICIENT | No FY2026 declared dividend is supplied. No statements or citations; do not infer zero dividend. |
| `fy2026-conflicting-constraint` | Capacity update + operations note | CONFLICTING | Transformer shortage versus software configuration capacity. Cite both; do not choose a winner or invent a timeline that resolves the disagreement. |
| `fy2026-european-pilot-budget` | Operations note | COMPLETE | EUR 12 million. Either identical original occurrence is an acceptable target. |
| `fy2026-battery-cabinet-window` | Operations note | COMPLETE | Procurement expects a shift into Q2 2027. Preserve the original line-break range; do not turn an expectation into a guarantee. |
| `fy2026-focused-constraint` | Capacity update | COMPLETE | Only the primary constraint: transformer shortage. The question explicitly excludes forecasts and other operational details. |
| `fy2026-supplier-lead-time-uncertainty` | Capacity update | COMPLETE | Improvement is expected in the first half of 2027, but the schedule is uncertain. Both timing and uncertainty must be retained. |
| `fy2025-revenue-and-fy2026-dividend` | Earnings brief | PARTIAL | Answer the supported revenue/growth part, not the absent dividend part. Do not suppress the entire answer or invent a dividend. |
| `fy2026-pilot-budget-and-battery-supplier` | Operations note | PARTIAL | Answer EUR 12 million; no named battery supplier is supplied. Do not mistake the delivery window for the supplier's identity. |
| `fy2026-pilot-budget-outside-scope` | Capacity update only | INSUFFICIENT | This document does not state the pilot budget. Do not borrow EUR 12 million from the excluded operations note. |
| `fy2025-revenue-and-fy2026-pilot-budget` | Earnings brief + operations note | COMPLETE | Revenue/growth and EUR 12 million; both evidence groups are required. Keep USD and EUR and their periods distinct. |

Totals: **12 cases**, with **7 COMPLETE, 2 PARTIAL, 1 CONFLICTING, and 2 INSUFFICIENT**.
The six added cases are deliberately small correctness/focus probes. They are not six additional
independent documents or a representative real-world benchmark.

For PARTIAL, the reference answer contains only supported facts. The existing application
contract keeps `missing_information=null` for accepted partial answers; the website shows its
fixed incomplete-evidence notice. INSUFFICIENT instead requires an explanation of the absent
evidence, with no statements or citations. A COMPLETE answer to either partial case is a coverage
error even when its supported facts are correct.

`expected_answer` is a semantic reference, not a requirement to reproduce its wording. The
existing scorer checks coverage and source-target mappings; it does not automatically prove that
the answer preserves uncertainty, answers every requested part, or avoids irrelevant prose.
Human review must assess those properties separately. Each evidence group is required; alternatives
inside one group are interchangeable. An exact, genuine citation outside the case's approved
targets fails its strict citation metric without automatically being a fabricated quotation.

## Review and execution boundary

1. Compare each expected fact, missing fact, scope, and source range with the original documents.
2. The owner explicitly accepted the expectations before any paid run of this catalog. Its
   recorded status is now `reviewed`. Later expectation changes need another reviewed version;
   do not alter targets to make an already measured response pass.
3. The preflight and live runner pin this reviewed catalog's digest and accept `--corpus closeout`.
   Request limits follow the twelve cases, but the USD 0.80 reservation ceiling is unchanged.
   It may stop a twelve-case run before completion; approval of the catalog is not permission
   to raise the cost ceiling. Retained reservations are conservative maximums, not actual spend.
4. Compare policies on this same reviewed version and record independent statement-support
   assessments separately from the application's model decisions.

The live runner defaults to the six-case `evaluation.json`. Selecting `--corpus closeout` explicitly
uses all twelve cases. Execution still requires `--execute`, the public-synthetic and Starter
confirmations, and a fresh ignored output filename; local raw-output inspection remains optional.
Reranking additionally requires allowance confirmation for up to twelve requests. No agent-run
provider execution, budget increase, default-policy adoption, frontend change, or new CI workflow
is approved by catalog review. Existing unit and PostgreSQL CI discovery verifies both profiles
with deterministic adapters.

Offline structure check from `backend/`:

```bash
uv run --locked python -m scripts.verify_m1_5_corpus \
  --manifest ../fixtures/m1_5/evaluation-closeout-v1.json --require-reviewed
```

Credential-free execution preflight from `backend/`:

```bash
uv run --locked python -m scripts.verify_m1_5_live_baseline \
  --corpus closeout --retrieval-policy rrf --answer-policy citable_units \
  --support-policy document_attribution
```

Passing these checks proves original-source integrity and bounded admission, not live retrieval
quality, independent support, or milestone acceptance. Six- and twelve-case scores have different
denominators; retain the recorded corpus ID and digest in every comparison.
