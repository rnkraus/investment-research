# First app evaluation on public sources

On 2026-11-11, the local app completed **21 requests across 19 reviewed cases** using 12 public
sources. **Four of nine cited answers met the full rubric. All 20 citation occurrences resolved
to exact source text.** Three of three unanswerable questions elicited an abstention, with a
generic message each time.

## Test setup

- **Corpus:** 12 selected sources, all indexed before the run. The six existing archive
  documents could appear in discovery when eligible under the requested filters. Each research
  question was limited to its predefined document scope.
- **Execution:** The evaluation called the same API endpoints as the web interface. Both model
  roles used GPT-5.6 Terra. The run recorded 12 generation calls, 17 evidence-check calls,
  14 query embedding calls, and no reranking calls in the app's usage ledger.
- **Scoring:** Expected results were fixed before the run. The review compared returned
  passages, claims, citations, and abstentions against those expectations. 🟢 means the case
  met the expectation, 🟠 means it met part of it, and 🔴 means it missed the target.
- **PDF preparation:** The IREN Q3 presentation exceeded the app's upload limit. The run used a
  compressed copy with all 35 pages and identical extracted text. The target page also rendered
  identically in a comparison at 95 dpi.

This single run describes performance on the reviewed cases. Rankings can vary with archive
contents, and generated answers can vary between runs.

## Archive discovery

**9 search requests** · 🟢 **5 met** · 🟠 **1 partly met** · 🔴 **3 missed**

Each mode is graded against the catalog target for that query. A red rating for a keyword paraphrase means the target passage was missed in that mode.

| Case | Mode | Result | Expected | Observed | Time |
| --- | --- | --- | --- | --- | ---: |
| D01 | Keyword | 🟢 Met | Exact NBIS company card. | NBIS card returned. | 0.039 s |
| D02 | Keyword | 🟢 Met | Bundey connection agreement on I-AU p. 1. | Correct passage at rank 1. | 0.083 s |
| D03 | Keyword | 🔴 Missed | GPU payback scenarios on I-Q1 p. 9. | No passages returned. | 0.075 s |
| D03 | Semantic | 🟢 Met | GPU payback scenarios on I-Q1 p. 9. | All three scenarios in the passage at rank 1. | 1.457 s |
| D04 | Keyword | 🔴 Missed | Citi facility purpose, lender, and maturity in N-CITI. | No passages returned. | 0.070 s |
| D04 | Semantic | 🟠 Partly met | Citi facility purpose, lender, and maturity in N-CITI. | Purpose at rank 1. Facility size and lender at rank 2. The maturity passage was absent from the 20 returned hits. | 0.379 s |
| D05 | Keyword | 🟢 Met | Scheduled Microsoft handoff on I-Q3 p. 9. Exclude I-H1 and I-Q1 by date. | Correct passage at rank 1. Both later documents were excluded. | 0.081 s |
| D06 | Keyword | 🟢 Met | Chanos's estimate with hits limited to @RealJimChanos. | Correct estimate at rank 1. The only returned passage was from the selected account. | 0.039 s |
| D07 | Keyword | 🔴 Missed | No documents or passages for Inferize within IREN. | The Nebius release was correctly excluded, but five IREN passages matched. PostgreSQL reduces “Inferize” and “inference” to the same stem. | 0.069 s |

## Cited answers

**9 answer cases** · 🟢 **4 met** · 🟠 **5 partly met** · 🔴 **0 missed**

The color grades each answer against the catalog. Three answers marked COMPLETE by the app still missed a required detail or attribution.

| Case | Result | App status | What was correct | What was missing | Time |
| --- | --- | --- | --- | --- | ---: |
| A01 | 🟢 Met | COMPLETE | Q1 group revenue of $399.0 million, Q2 of $582.3 million, and 46% sequential growth. Both letters cited. | — | 9.002 s |
| A02 | 🟠 Partly met | PARTIAL | August delivery and acceptance by Microsoft. | The May schedule for calendar Q3 2026. Its indexed passage was absent from the retrieved candidates. | 4.365 s |
| A03 | 🟢 Met | COMPLETE | $2.8 billion in contracts, distinct from the increase in the ARR target from $3.7 billion to over $4 billion. | — | 4.175 s |
| A04 | 🟢 Met | COMPLETE | Matched 0%, 50%, and 90% prepayment to 2.4, 1.5, and 0.4 years. | — | 3.315 s |
| A05 | 🟠 Partly met | PARTIAL | $3.5 billion facility provided by Citi. | 364-day maturity and initial spread of 2.75 percentage points over Term SOFR. Their indexed passage was absent from the retrieved candidates. | 3.165 s |
| A06 | 🟠 Partly met | COMPLETE | Named Token Factory and explained the cost of keeping spare GPUs running. | How reducing model cold starts helps avoid idle GPU time. Its indexed passage was absent from the retrieved candidates. | 5.228 s |
| A07 | 🟢 Met | COMPLETE | Attributed operating cash flow relative to EBITDA to Baker, and lower funding needs and faster payback to IREN's illustration. | — | 6.032 s |
| A08 | 🟠 Partly met | COMPLETE | Reported roughly tenfold usage when inference is 50% cheaper and cited the X post. | Explicit attribution to the Wall St Engine post in the answer text. | 2.786 s |
| A09 | 🟠 Partly met | COMPLETE | $1 billion in operating ARR as of September 30 and the possible extension to the end of February. | The exact target date, January 31, 2027. The answer said January 2027 even though it cited the exact date. | 4.369 s |

All 20 citation occurrences resolved inside the requested document scope. The 13 PDF excerpts matched the extracted text of their physical pages exactly. The seven plain-text excerpts matched the original character ranges exactly. The cited text supported the claims that were returned. Completeness and explicit attribution were graded separately above.

## Insufficient evidence

**3 abstention cases** · 🟢 **0 met** · 🟠 **3 partly met** · 🔴 **0 missed**

The catalog requires both abstention and a specific explanation. All three cases correctly returned INSUFFICIENT with the same generic message.

| Case | Result | What was correct | What was missing from the message | Time |
| --- | --- | --- | --- | ---: |
| U01 | 🟠 Partly met | Withheld the exact Inferize purchase price. | The release says the transaction terms were undisclosed. | 2.038 s |
| U02 | 🟠 Partly met | Withheld the exact Bundey energization date. | The source gives a projected start from 2028 and no observed energization date. | 1.881 s |
| U03 | 🟠 Partly met | Withheld the exact operating ARR for November 6. | The selected sources give no reported ARR for November 6. The $1 billion figure is dated September 30. | 2.313 s |

## Latency and usage

The run took 51 seconds, including citation lookups and local recording. Individual request times exclude the later citation lookups. Median cited-answer latency was 4.365 seconds (2.786–9.002 seconds). Median abstention latency was 2.038 seconds.

| Recorded provider operation | Calls | Usage-based USD estimate |
| --- | ---: | ---: |
| Answer generation | 12 | $0.102480 |
| Statement support | 17 | $0.025726 |
| Research question embedding | 12 | $0.000012 |
| Semantic archive query embedding | 2 | $0.000002 |
| **Total for the 21 requests** | **43** | **$0.128220** |
| Document embedding during preparation | 18 | $0.000918 |

Costs are estimates from the app's recorded provider usage. They exclude Pinecone and
infrastructure costs. The provider invoice is the billing record.

## Failure analysis

- **A05 — Retrieval missed a required passage.** The indexed Citi release states the facility
  maturity and initial interest spread. Neither retrieval lane returned the passage containing
  those details for this question, so the answer gave only the facility size and lender.
  [Nebius Citi release][n-citi]
- **A09 — The answer lost precision.** The IREN presentation gave January 31, 2027, and the app
  cited that passage. The generated answer said January 2027. The citation supported the
  month, while the rubric required the exact date. [IREN Q1 FY2027 presentation][i-q1]

## Source references

| ID | Publisher or author |
| --- | --- |
| I-Q3 | [IREN Q3 FY2026 results presentation][i-q3] |
| I-AU | [IREN Australian data center campus release][i-au] |
| I-NEW | [IREN new customer contracts release][i-new] |
| I-H1 | [IREN Horizon 1 delivery release][i-h1] |
| I-Q1 | [IREN Q1 FY2027 results presentation][i-q1] |
| N-Q1 | [Nebius Q1 2026 shareholder letter][n-q1] |
| N-Q2 | [Nebius Q2 2026 shareholder letter][n-q2] |
| N-INF | [Nebius Inferize release][n-inf] |
| N-CITI | [Nebius Citi financing release][n-citi] |
| X-CHANOS | [James Chanos on IREN capital employed][x-chanos] |
| X-BAKER | [Gavin Baker on GPU prepayments][x-baker] |
| X-WSE | [Wall St Engine on AWS inference][x-wse] |

The X links are the original post references recorded with the local evaluation copies.
Direct access to those posts could not be verified when preparing this report.

[i-q3]: https://iren.gcs-web.com/static-files/2289f56f-f0f8-4673-a7d4-2359afe93eb6
[i-au]: https://iren.gcs-web.com/static-files/89f56bc3-0473-4a1b-a863-c6a3dba11018
[i-new]: https://iren.gcs-web.com/static-files/d6b51979-3c78-46fb-8fae-6e6acefce19c
[i-h1]: https://iren.gcs-web.com/static-files/49325cad-f6fe-42ba-acd6-b286ede0fb4a
[i-q1]: https://iren.gcs-web.com/static-files/f80da6ea-9d04-489c-ba7f-52f4b38e6680
[n-q1]: https://assets.nebius.com/assets/aa1bc2e6-df83-40cd-a6a2-95e7cda3d16c/Nebius%20SHL_Q1%202026.pdf?cache-buster=2026-05-13T14%3A00%3A35.352Z
[n-q2]: https://assets.nebius.com/assets/4462517b-ce83-41f2-96ed-f2ac1bc06a05/SHLQ226%20%281%29.pdf?cache-buster=2026-08-12T11%3A57%3A07.944Z
[n-inf]: https://nebius.com/newsroom/nebius-acquires-inferize-to-strengthen-nebius-token-factorys-production-inference-stack
[n-citi]: https://nebius.com/newsroom/nebius-secures-3-5-billion-financing-facility-with-citi
[x-chanos]: https://x.com/RealJimChanos/status/2118499794968612966
[x-baker]: https://x.com/GavinSBaker/status/2120298831552327680
[x-wse]: https://x.com/wallstengine/status/2120554882696724835
