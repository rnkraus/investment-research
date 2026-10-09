# M1.5 closeout question review

**Status:** Versions 1 and 2 were owner reviewed and accepted on 2026-10-09. The owner approved
the version-2 budget-question correction after seeing its exact wording and unchanged COMPLETE
expectation. Neither catalog approval is live answer-quality or M1.5 acceptance.

The machine-readable expectations are in frozen
[`evaluation-closeout-v1.json`](./evaluation-closeout-v1.json), catalog ID
`m1.5-rag-quality-closeout-v1`, and separately reviewed
[`evaluation-closeout-v2.json`](./evaluation-closeout-v2.json), catalog ID
`m1.5-rag-quality-closeout-v2`. Both reuse the same three fictional CC0 originals; v1 preserves
all six cases and six targets from the reviewed [`evaluation.json`](./evaluation.json).
One additional exact target covers the supplier lead-time expectation in the capacity update.
The historical six-case reports and their scores remain unchanged.

## Version-2 budget-question correction — reviewed

[`evaluation-closeout-v2.json`](./evaluation-closeout-v2.json), catalog ID
`m1.5-rag-quality-closeout-v2`, preserves all originals, seven source targets, scopes, expected
answers, coverage labels, and the first eleven cases from version 1. Only the final question and
its ID change, besides catalog identity and review metadata:

| Field | Frozen version 1 | Reviewed version 2 |
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

The owner accepted this exact wording and unchanged evidence expectations. The live runner maps
`--corpus closeout` to reviewed version 1 and `--corpus closeout-v2` to reviewed version 2, pinned
at SHA-256 `659b3f0704cca5465c00e547cc1a423b7bf1b05504c8e89401633ce48c15162e`.
Paid runs still require the existing explicit execution confirmations and bounded budget.
Do not overwrite historical reports, recompute their recorded scores, or compare
the changed question as an unchanged paired experiment. These are developer-informed probes,
not held-out quality evidence.

Check the reviewed version's schema and original-source integrity from `backend/`:

```bash
uv run --locked python -m scripts.verify_m1_5_corpus \
  --manifest ../fixtures/m1_5/evaluation-closeout-v2.json --require-reviewed
```

The CLI must still reject any unreviewed catalog copy. Counts remain twelve cases: seven COMPLETE,
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

## Prompt-v6 live observation and qualitative source review — 2026-10-09

**Review boundary:** The owner executed the live test and supplied its local diagnostic output.
Codex separately compared the ten published answers (14 statements) and two abstentions with the
reviewed version-2 expectations and all three originals, including all four PDF pages. This is
a qualitative developer-agent review outside the application's support evaluator, not owner
acceptance, a human review, a held-out benchmark, or a structured independent-model evaluation.
The supplied evaluator reasons were not treated as proof of statement support.

The ignored local report is `closeout-v2-luna-luna-prompt-v6-20261009-042912.json` under
`outputs/m1_5/`, SHA-256
`f43a0a5ad3b67d224b2a2d18c2b00662da788b569c1ab9d4311d82707415d88a`.
Keep it unchanged; it is not committed and its structured independent-support fields remain
pending. Publicly shareable findings are recorded here without prompts, raw model bodies,
provider credentials, private hosts, or machine-specific attachment paths.

The run used reviewed closeout v2 (digest recorded above), `m1-5-rrf-k60-v1`, Luna/Luna,
`m1-5-citable-units-en-answer-prompt-v6`, and the diagnostic support profile
`m1-5-local-document-attribution-support-reason-v1`. Reranking was disabled. Asking the support
model for reasons changes its prompt/schema; do not present this as a measurement of the normal
Boolean-only support profile.

| Case suffix | Published statements | Source-based assessment |
| --- | --- | --- |
| `fy2025-revenue` | 2 | PDF page 2 supports USD 184.2 million, USD 151.0 million, both fiscal years, and 22.0% growth. |
| `fy2026-rollout-constraint` | 1 | Capacity update supports transformer shortage; no unsolicited forecast is published. |
| `fy2026-dividend` | 0 | No scoped original establishes a declared FY2026 dividend; uncited abstention is appropriate. |
| `fy2026-conflicting-constraint` | 2 | The two July 22 reports support separately attributed, incompatible constraints; neither is chosen as the winner. |
| `fy2026-european-pilot-budget` | 1 | Operations note supports EUR 12 million without assigning a fiscal period. |
| `fy2026-battery-cabinet-window` | 1 | Operations note supports an expected shift into Q2 2027, not a guaranteed date. |
| `fy2026-focused-constraint` | 1 | Only the capacity update's primary constraint is returned, as requested. |
| `fy2026-supplier-lead-time-uncertainty` | 2 | Both the first-half-2027 expectation and schedule uncertainty are preserved. |
| `fy2025-revenue-and-fy2026-dividend` | 1 | PARTIAL returns supported revenue/growth without inventing a dividend. |
| `fy2026-pilot-budget-and-battery-supplier` | 1 | PARTIAL returns the budget without inventing a named supplier. |
| `fy2026-pilot-budget-outside-scope` | 0 | Capacity update alone lacks the budget; the excluded operations note is not used. |
| `fy2025-revenue-and-european-pilot-budget` | 2 | Both originals support their respective facts; currencies stay separate and no FY2026 budget applicability is inferred. |

No unsupported material claim was identified in the 14 published statements. Unvalidated
`rendered_text` is not the published answer: review the `validated_result` assembled from accepted
statements. In the partial supplier case, additional model-authored missing-information prose
was absent from that published answer; PARTIAL retains the existing fixed UI notice.

**Citation distinction:** Preserve strict target matching at 10/12. The conflict case has two
additional genuine title/date citations; the combined revenue/budget case has one additional
genuine PDF attribution citation. All required factual targets are present in both cases. These
extras are not fabricated evidence or substitutes for factual support, but remain outside the
frozen strict targets. Their exact validity comes from the report's canonical checks; this
qualitative reading does not independently verify database UUID/offset mapping, which repeated
budget occurrence was stored, or source-viewer opening.

Measured coverage is 12/12, required factual evidence presence is 10/10 answerable cases, exact
citation validity is 10/10 checked answers, and uncited abstention is 2/2. Median question latency
is 4,895 ms and maximum latency is 10,411 ms. Estimated question-phase cost is USD 0.010788;
including preparation it is USD 0.010813. Reserved USD 0.138298 is a conservative reservation,
not an actual charge. Estimates use repository prices, exclude Pinecone billing, and are not
provider invoices. Cleanup was VERIFIED and execution failures were 0/12.

This single developer-informed synthetic run does not establish general reliability or isolate
the effect of the new prompt. The changed version-2 budget question is not an unchanged paired
comparison with version 1. Owner review of these findings, current-revision runtime/source-opening
and hosted checks, an explicit profile-adoption decision, and portfolio limitations remain
separate closeout gates. No report, score, catalog, application default, or acceptance flag is
changed by recording this review.
