# Quarter comparison retrieval and answer evaluation

On 2026-11-25, the scoped quarter follow-up raised complete evidence coverage from **17 of 20
to 20 of 20** answerable development cases in an offline replay. The three changed cases
then passed a separate live answer and evidence-check run.

## Retrieval replay

- The baseline searched the whole local archive of 53 searchable documents. Replay reused
  the recorded keyword and vector candidates and read additional canonical passages from
  the same archive. It made zero provider calls.
- The selector looked for one numerical passage per requested reporting quarter and, when
  requested, a passage linking the sequential change to the selected metric amount. The
  eight-chunk limit and generation-input checks applied to both selections.
- A case counted as complete when the final context contained all predefined evidence
  markers on the expected original pages. The 17 already complete contexts remained
  identical. Only Meta, NVIDIA, and TSMC gained context.

## Targeted live answer test

The live run used the same 53-document archive, hybrid retrieval with RRF, the quarter
follow-up, and GPT-5.6 Terra for answer generation and batched evidence checking. Luna
reranking was disabled. Each case ran once and used eight chunks.

| Case | Material result | Cited originals |
| --- | --- | --- |
| A30 · Meta capital expenditure outlook | Q2 guidance of USD 130–145 billion narrowed to USD 135–145 billion in Q3. Both ranges include principal payments on finance leases. | Page 2 of each release |
| A33 · NVIDIA Data Center revenue | USD 89.0 billion in Q2 FY2027 and USD 107.6 billion in Q3 FY2027, with reported sequential growth of 21%. | Q2 page 1 and Q3 pages 1 and 3 |
| A38 · TSMC wafer revenue mix | The 2-nanometer share rose from 3% in Q2 2026 to 8% in Q3, a gain of five percentage points. | Page 1 of each release |

All three answers met the reviewed content expectations and passed production validation.
The seven distinct excerpts matched the extracted originals exactly. These excerpts supplied
eleven statement-to-evidence links. The run made three embedding requests, three Pinecone
queries, three answer calls, and three batched evidence-check calls. Estimated model cost was
USD 0.071425 from recorded token usage and the application's price policy, excluding Pinecone
charges.

The **20 of 20** score measures retrieval coverage on the development cases. Live answer
results apply to the three changed cases. The reserved evaluation cases and two cases with
unsearchable required PDFs were outside this run. Original documents, raw outputs, and
runtime identifiers remain local.
