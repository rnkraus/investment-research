# M1.5 closeout question review

**Status:** Pending owner review. No live run or M1.5 acceptance is recorded by this document.

The machine-readable source of truth is
[`evaluation-closeout-v1.json`](./evaluation-closeout-v1.json), catalog ID
`m1.5-rag-quality-closeout-v1`. It reuses the same three fictional CC0 originals and preserves
all six cases and six targets from the reviewed [`evaluation.json`](./evaluation.json).
One additional exact target covers the supplier lead-time expectation in the capacity update.
The historical six-case reports and their scores remain unchanged.

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
2. Approve or correct the expectations before any paid run of this catalog. Keep
   `pending_owner_review` until the owner explicitly accepts them.
3. After approval, record the reviewed catalog digest and update the bounded evaluation tooling
   in a separate work block. Recalculate request/reservation limits for twelve questions rather
   than assuming that the six-case USD 0.80 limit will admit them all.
4. Compare policies on this same reviewed version and record independent statement-support
   assessments separately from the application's model decisions.

The current live runner still pins `evaluation.json`; it does **not** execute this draft. No new
provider command, paid budget, default-policy adoption, frontend change, or CI workflow is approved
by creating it. Existing unit-test discovery verifies the structural draft checks without credentials.

Offline structure check from `backend/`:

```bash
uv run --locked python -m scripts.verify_m1_5_corpus \
  --manifest ../fixtures/m1_5/evaluation-closeout-v1.json
```

Adding `--require-reviewed` must reject this pending draft. Passing the structure check proves
original-source integrity, not live retrieval quality, independent support, or milestone acceptance.
