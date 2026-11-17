# Neighbor context app evaluation

On 2026-11-17, the local HTTP API completed the 19 reviewed portfolio cases in 21 requests.
All nine answer cases met their material content expectations. All three unanswerable cases
withheld an answer, while retaining the generic insufficient-evidence message. All 24 returned
citation occurrences matched their immutable originals exactly.

## Configuration and method

- The runtime used Terra for answer generation and batched evidence checking, the existing
  small embedding model, standard hybrid RRF, page expansion, and the new bounded text-neighbor
  selector. The lexical strategy was the existing `websearch` default. Reranking was disabled.
- Research questions used the original catalog wording and the whole archive of 18 public or
  synthetic documents. The 12 catalog originals were verified against their reviewed checksums
  and current searchable versions before the run. Discovery retained its catalog metadata filters.
- Neighbor selection examines up to two positions on either side of the first four text anchors.
  An informative single neighbor can bring its intervening paragraph when the two share at least
  two terms absent from the anchor. At most two additions replace trailing context. The context
  remains bounded by eight chunks, the character budget, and generation preflight.
- This is one end-to-end measurement. The original published app baseline used predefined
  document scopes and an earlier configuration. It cannot isolate this selector's contribution.
  The preceding controlled retrieval experiment reused frozen dense hits and found the missing
  A06 pair without losing a catalog evidence marker.
- The new text, page, company-priority, and Luna selection identifiers end in `v2`.
  Persistence and evidence qualification retain support for their historical `v1` identifiers.

## Answer review

The review compared returned claims and their cited original text with
[catalog v1](portfolio-cases-v1.json). Answer completeness, exact quote resolution, and
insufficient-evidence behavior were assessed separately from the app's COMPLETE status.

| Case | Review | Observed material content |
| --- | --- | --- |
| A01 | Met | Q1 group revenue of $399.0 million, Q2 of $582.3 million, and 46% sequential growth. |
| A02 | Met | May schedule for calendar Q3 2026 and the August 13 delivery and acceptance. |
| A03 | Met | $2.8 billion contract value and the distinct ARR target increase from $3.7 billion to over $4 billion. |
| A04 | Met | GPU cash payback of 2.4, 1.5, and 0.4 years for 0%, 50%, and 90% prepayments. |
| A05 | Met | Citi's $3.5 billion facility, 364-day maturity, and initial Term SOFR plus 2.75% rate. |
| A06 | Met | Token Factory, cold starts, and the link between startup time and idle GPU costs. The claim cites both neighboring original paragraphs. |
| A07 | Met | Baker's OCF-to-EBITDA metric and IREN's separate funding and payback illustration, attributed to their respective sources. |
| A08 | Met | Explicit attribution to the Wall St Engine post reporting Garman's order-of-magnitude usage observation when inference becomes 50% cheaper. |
| A09 | Met | $1.0 billion operating ARR as of September 30, the January 31, 2027 target, and possible extension to the end of February. |

All nine answers passed the production evidence checks. All 24 citations opened and their
unchanged excerpts matched the extracted PDF page or exact text-original character range.
Document-prefix attribution remains part of the evidence-check contract. Exact quote resolution
alone is not a measure of claim completeness. A09 used eligible passages on pages 22 and 4;
page 22 supplied the exact target day and extension, so the review accepted this alternative
to the catalog's suggested page 24 note.

| Case | Abstention | Explanation |
| --- | --- | --- |
| U01 | Correctly withheld the acquisition price. | Generic message; it did not explain that the source calls the terms undisclosed. |
| U02 | Correctly withheld an observed energization date. | Generic message; it did not distinguish projected commencement from 2028. |
| U03 | Correctly withheld ARR for November 6. | Generic message; it did not explain that the reported figure belongs to September 30. |

## Archive discovery

Seven discovery cases produced nine search requests. Five requests met the target, one partly
met it, and three missed it. This selector changes research context selection; standalone
discovery kept its existing behavior.

| Case | Mode | Review | Observation |
| --- | --- | --- | --- |
| D01 | Keyword | Met | Exact NBIS company card. |
| D02 | Keyword | Met | The Bundey agreement appeared at rank 1. |
| D03 | Keyword | Missed | No passage matched the payback paraphrase. |
| D03 | Semantic | Met | The GPU-payback page appeared at rank 1. |
| D04 | Keyword | Missed | No passage matched the financing paraphrase. |
| D04 | Semantic | Partly met | Purpose at rank 1 and lender/size at rank 2; maturity remained absent. |
| D05 | Keyword | Met | The May Horizon 1 schedule appeared at rank 1 under the publication cutoff. |
| D06 | Keyword | Met | The capital-employed estimate appeared at rank 1 within the selected handle. |
| D07 | Keyword | Missed | Five IREN passages matched the stem shared by Inferize and inference. The Nebius source was excluded. |

## Usage and verification

The usage ledger recorded 12 answer-generation calls, nine batched support calls, and 12 research
query embeddings. Discovery added two semantic search requests. Total usage-based estimated cost
was USD 0.179516, below the USD 0.80 run ceiling. This is a calculation from recorded provider
usage and the project's price table. Median measured answer latency was 6.57 seconds across eight
cases; A01's HTTP timing was unavailable after a local report-field correction. Its saved stream
was reused and the request was not repeated.

The first controlled A06 run with both neighbors was rejected. A subsequent repeat and this
integrated run were accepted. These observations demonstrate successful evidence coverage and
also retain the observed generation/evaluation variability.

Deterministic verification completed with 2,854 backend unit tests, 429 PostgreSQL integration
tests (seven skipped), and 100 frontend tests. Backend formatting, linting, and type checking
passed. The first restricted run hit TLS initialization errors in three lifecycle tests; the
complete rerun passed. Raw HTTP streams, source identifiers, and full passages remain local
under ignored outputs.
