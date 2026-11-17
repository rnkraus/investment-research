# Current RAG and archive-search evaluation

On 2026-11-17, one local HTTP evaluation ran all 19 reviewed portfolio cases against the
18-document archive. All nine answer cases met their material content expectations. Two of
three abstention cases met the expected behavior; U03 instead returned a partial answer using
an undated-in-the-answer historical ARR claim. The run also exercised the new hybrid archive
search on the two discovery paraphrases.

## Configuration and method

- Revision: `aa96c44ec1a03553f09cd35be12957c2a437a117`.
- Dataset: [catalog v1](portfolio-cases-v1.json), SHA-256
  `5f7f40e168ebfb0fc4899debac9d9636ac0f33149de3193dc527bee3b6bfcbaa`.
- Research used the original questions and the whole archive, Terra for both model roles,
  batched evidence checking, the existing small embedding model, standard hybrid RRF,
  company prioritization, page expansion, and bounded text neighbors. Query decomposition,
  reranking, and experimental weighted OR were disabled.
- Context limits were eight chunks, 24,000 characters, and 18,000 bytes of generation-input
  overhead. The running API source files and RAG configuration matched the checkout.
- The twelve catalog originals matched their reviewed checksums and current searchable versions.
  The six additional archive documents were the previously approved public/synthetic sources.
  Current document versions were checked before each catalog case.
- The run issued 23 requests: twelve research requests and eleven discovery requests. D03 and
  D04 each used keyword, semantic, and hybrid search; other discovery cases retained keyword
  search and their reviewed company, date, or author filters.
- Raw streams, passage text, runtime identifiers, source mappings, and the local runner remain
  ignored. No application behavior, model, or index configuration changed during this test.

## Research results

The review compared returned claims and cited original text with the catalog expectations,
separately from the model's coverage label and production evidence-check result.

| Case | Review | Observed material content |
| --- | --- | --- |
| A01 | Met | Q1 group revenue of $399.0 million, Q2 of $582.3 million, and 46% sequential growth. |
| A02 | Met | Calendar Q3 2026 planned handoff and delivery/acceptance announced on August 13. |
| A03 | Met | $2.8 billion contract value; year-end ARR target raised from $3.7 billion to over $4 billion. |
| A04 | Met | Illustrative GPU cash payback of 2.4, 1.5, and 0.4 years at zero, 50%, and 90% prepayment. |
| A05 | Met | Citi's $3.5 billion facility, 364-day maturity, and initial Term SOFR plus 2.75% rate. |
| A06 | Met | Token Factory and the cold-start/idle-GPU cost mechanism, citing the neighboring paragraphs. |
| A07 | Met | Baker's OCF-to-EBITDA metric and IREN's separately attributed funding/payback illustration. |
| A08 | Met | Wall St Engine attribution for the reported Garman observation about cheaper inference and usage. |
| A09 | Met | September 30 operating ARR of $1 billion, January 31 target, and possible end-February extension. |
| U01 | Met | Withheld the undisclosed acquisition price; generic insufficient-evidence message. |
| U02 | Met | Withheld an observed Bundey energization date; generic insufficient-evidence message. |
| U03 | Missed | Returned PARTIAL with an earlier report's $1 billion operating ARR "today" claim, without evidence for November 6. |

All nine answer cases were accepted by production validation. They returned 23 citation
occurrences; U03 added one. All 24 excerpts matched their immutable originals exactly, using
PDF page extraction or original-text offsets and quote hashes. Exact matching did not establish
date relevance: U03's accepted quote did not answer the requested observation date.

For A06, the final context contained the platform at rank 1, cold-start passage at rank 2,
and GPU-cost mechanism at rank 3. A09's page 22 was at rank 8 and supplied the reported value,
exact target date, and extension. These observations confirm the needed evidence reached the
answer model in this run.

U03 asked for operating ARR reported for November 6, 2026. The accepted statement instead said
that IREN's FY26 results material reported $1 billion "today." Its cited original was outside
the twelve catalog sources but within the approved whole archive. The answer did not explicitly
assign that value to November 6; nevertheless, it neither answered that date nor gave the
catalog's acceptable September 30 figure with its date. It therefore fails the reviewed rubric.
The [preceding run](neighbor-context-app-run.md) abstained on U03. This single repeat records
variability, not evidence that the archive-search addition caused the changed research outcome.

## Archive discovery results

| Case | Mode | Review | Observation |
| --- | --- | --- | --- |
| D01 | Keyword | Met | Exact NBIS ticker company card. |
| D02 | Keyword | Met | Bundey transmission agreement at rank 1. |
| D03 | Keyword | Missed | No passage matched the GPU-payback paraphrase. |
| D03 | Semantic | Met | Expected GPU-payback page at rank 1. |
| D03 | Hybrid | Met | Expected GPU-payback page at rank 1. |
| D04 | Keyword | Missed | No passage matched the financing paraphrase. |
| D04 | Semantic | Partly met | Purpose at rank 1 and lender/size at rank 2; maturity absent. |
| D04 | Hybrid | Partly met | Purpose at rank 1 and lender/size at rank 2; maturity absent. |
| D05 | Keyword | Met | May Horizon 1 schedule at rank 1 under the publication cutoff. |
| D06 | Keyword | Met | Capital-employed estimate at rank 1; only the selected handle's source returned. |
| D07 | Keyword | Missed | Five IREN stem matches for Inferize/inference; no exact Inferize result. |

Six of eleven discovery requests met the expectation, two partly met it, and three missed it.
On the original nine requests the breakdown remains five met, one partial, and three missed.
The two additional hybrid requests contribute one met and one partial result; they do not show
an improvement over semantic search on these two questions. Discovery returns up to twenty
previews and does not generate or validate an answer. It is not scored as an eight-chunk RAG
context, and a target-source hit alone does not establish complete evidence coverage.

## Usage and interpretation

The ledger recorded 38 successful provider operations: twelve research query embeddings,
four archive query embeddings, twelve answer calls, and ten batched support calls. The two
insufficient answers required no support call. Estimated usage cost was USD 0.187648, below
the USD 0.80 run ceiling. This uses recorded provider usage and the application's price table,
not the provider's billing statement or reserved budget. Median elapsed time across the nine
answer cases was 5.183 seconds, including local citation inspection overhead.

This is one end-to-end evaluation on a small, repeatedly used development catalog. It is not a
held-out quality estimate, a load test, or an embedding-model comparison. The observed gaps
are date-specific answer relevance, strict lexical paraphrase matching, stemming false positives,
and incomplete discovery of Citi terms. They do not establish that larger embeddings are needed.
The next agreed evaluation uses substantially more approved sources and newly reviewed questions
with the current embedding model before considering a model change.
