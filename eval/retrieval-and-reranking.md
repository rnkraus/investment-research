# Retrieval and reranking follow-up

The standard app search and the same search with GPT-5.6 Luna reranking both put all
required evidence in the context for **9 of 9 answerable catalog questions**. Luna moved
several key passages higher and surfaced six additional named locations for a separate
IREN question. The paired comparison measured retrieval coverage and rank. It did not
generate or validate answers.

## How the comparison ran

The local archive contained 18 documents. The replay covered the nine answerable and three
unanswerable research cases from the [first evaluation](first-app-run.md), plus the question
“Which data center locations does IREN have?” The Inferize question explicitly asked how
shorter model startup reduces idle GPU time. The seven discovery cases were outside this
comparison because they do not use the answer context.

Standard search replayed previously recorded Pinecone hits and reran PostgreSQL retrieval
with RRF, company prioritization, page search, dated lookups, and text neighbors where
applicable. For each question, the shared candidate pool contained the first 20 fused hits
and any additional passages selected by the app, for a total of 20–24 passages. The control
was the eight passages prepared by the app. Luna received the same candidate pool and
returned eight passage IDs. Its selections met the character limit. Generation preflight
was not rerun on those alternative contexts.

The comparison used 13 Luna calls, 98,917 input tokens, and 351 output tokens. The recorded
usage-based estimate was **USD 0.0202**. There were no new embedding, Pinecone, answer
generation, or evidence-check calls in this comparison.

## Where the evidence landed

Ranks are positions in the final eight-passage context. All required evidence in the
standard contexts remained available after reranking.

| Case | Required evidence | Standard → Luna |
| --- | --- | --- |
| A01 · Nebius revenue | Q1 revenue, then Q2 revenue and sequential growth | 3 → 1 and 1 → 2 |
| A02 · Horizon 1 | May handoff plan, then August delivery and acceptance | 5 → 1 and 1 → 2 |
| A03 · IREN contracts | July contract value and change to the ARR target | 1 → 1 |
| A04 · GPU payback | All three illustrative prepayment scenarios | 1 → 1 |
| A05 · Citi financing | Facility amount and lender, then maturity and initial spread | 1 → 1 and 5 → 2 |
| A06 · Inferize | Token Factory, cold-start mechanism, and idle-GPU effect | 4 → 1, 5 → 3, and 6 → 4 |
| A07 · GPU financing | Baker's cash-flow metric, then IREN's illustrated funding and payback benefits | 3 → 1 and 2 → 2 |
| A08 · Inference usage | Garman's usage statement as reported by Wall St Engine | 1 → 1 |
| A09 · IREN operating ARR | September operating ARR and the exact January target date | 8 → 1 |

An earlier comparison isolated RRF on 20 saved candidates and measured **5/9 → 6/9**
fully covered cases with Luna. The current standard app already supplies passages that
were missing from that earlier pool, including the Horizon handoff plan, Citi terms, and
the Inferize cold-start paragraph.

### IREN data center locations

Standard search selected the Bundey release and three Nebius passages. Luna kept Bundey,
dropped the three Nebius passages, and promoted page 27 of IREN's Q3 FY2026 earnings
presentation from fused rank 16 to context rank 1. That page names Childress, Sweetwater,
Kiowa, Mackenzie, Canal Flats, and Prince George. It lists 490 MW in the Spanish power
portfolio. Badajoz was absent from the shared candidate pool. The portfolio listing does
not establish which facilities were operational.

In a separate single-question test with 21 fused candidates, RRF and the Pinecone
cross-encoder both selected Bundey as their only specifically named site. Luna selected
the broader IREN overview. The cross-encoder was not run on the larger comparison.

### Questions with insufficient evidence

| Case | Retrieval observation |
| --- | --- |
| U01 · Inferize purchase price | Both contexts lacked a purchase price and the explicit statement that terms were undisclosed. |
| U02 · Bundey energization | Luna moved the expected start from 2028 from rank 3 to 1 and removed two passages about other sites. Neither context established an actual date. |
| U03 · IREN ARR on November 6 | Luna moved an older August figure from rank 6 to 2. The dated third-party post about a future target remained at rank 7. Neither context established operating ARR for November 6. |

## Earlier retrieval experiments

Each row describes its own comparison. The answer scores come from reviewed model
answers. The replay observations describe the passages selected by the search.

| Technique | Measured result | Status |
| --- | --- | --- |
| Manual query decomposition | **4/8 → 7/8** complete answers on multipart questions. Short lexical subqueries recovered the Horizon 1 schedule, Citi terms, and operating ARR evidence. | Manual experiment |
| Automatic query decomposition | **5/11 → 6/11** complete answers, tied at **4/8** on the original multipart questions. The additional success was a new Nebius capacity question. | Optional mode |
| Parent-page expansion | Expanding around existing chunk hits left the Horizon 1 schedule and the September operating ARR pages outside the context. | Experiment |
| Direct page retrieval | **5/9 → 7/9** complete factual answers against RRF. Searching complete pages brought the May Horizon 1 schedule and the Citi maturity and spread into the eight passages. A09 gained the target-date evidence but still lacked the September figure. | Standard search |
| Model-guided evidence follow-up | **7/9 → 7/9** complete factual answers. The proposed A09 query retrieved page 32, which discussed operating expenses, while the required operating ARR figure was on page 22. | Experiment |
| Metric-weighted lookup and date guard | Weighting the named metric moved A09 page 22 from rank 8 to 1 and page 32 from 1 to 3 in the targeted ranking replay. The follow-up initially regressed on U03 by surfacing a figure for a different date. Preserving the requested date restored abstention, with **8/9** complete factual answers and **3/3** safe abstentions in the replay with matched live results. | Standard search |
| Text-neighbor expansion | For A06, the context gained both paragraphs connecting model cold starts to idle GPUs and explaining Inferize's utilization benefit. The targeted live answer passed validation. The later app replay matched that successful context. | Standard search |
| Company prioritization | The bounded bonus preserved third-party evidence. An NVIDIA query selected the IREN Horizon 1 release at rank 1, and an explicit Nebius filter returned only Nebius passages. The replay of the broad IREN location question kept the same eight passages with the bonus alone. | Standard search |
| Source-derived location follow-up | A local prototype derived place names from company overview passages. Combined with company prioritization, it selected Childress, Sweetwater, and Badajoz and reduced Nebius passages from three to zero. This test covered one question and made no model calls. | Experiment |

The deterministic page, date, and neighbor changes were replayed across all 12 research
cases without new provider calls. Previously recorded live outcomes were reused where
the question, applicable prompt, and ordered context matched. The separate discovery
results were reused. The factual scores for these experiments record A08's explicit
secondary-source attribution separately from its factual content.

## Separate runtime observations

- **Bundey abstention.** A manual website check of the exact Bundey energization date
  returned **Insufficient evidence**, the expected result. This exercised abstention.
- **Rejected-claim display.** Automated API and PostgreSQL checks verified that a rejected
  answer in the normal source-ID profile can show the claim, its selected evidence, and
  the evidence-check model's brief explanation. The Bundey website run did not exercise
  this display because it abstained.
- **Hybrid context limit.** In a separate four-document holdout, the Nebius ARR question
  exceeded the citation metadata limit with eight passages. The context fitter removed
  the lowest-ranked passage and retained the evidence on page 8 at rank 1. The single live
  check then returned the December and September figures with verified citations.
- **Hybrid search on held-out PDFs.** On four PDFs and eight new questions, both the
  lexical path with page and date search and that same path with Pinecone and RRF
  retained every required fact for **5/5** answerable questions. Before context fitting,
  **5/5 versus 4/5** passed generation preflight. In three selected live hybrid checks,
  the Nostrum–Badajoz comparison and Nebius quarterly figures had complete cited
  answers, and the unsupported January revenue question correctly abstained.
