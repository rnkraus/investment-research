# Investment Research Archive — Product Scope

**Status:** Draft v0.1  
**Product stage:** Production-capable portfolio release  
**Initial UI language:** English

**Document languages:** English in V1; additional languages may be added in later reviewed increments

## 1. Product goal

Investment Research Archive turns PDFs and pasted text into a private, searchable research archive. Users can find information, ask questions, and inspect the exact evidence behind every archive-based answer.

The product serves three purposes:

- support real personal investment research;
- demonstrate production-grade Applied AI engineering;
- provide a reusable foundation for future client projects.

**Core promise:** Archive-derived statements are backed by inspectable evidence. Missing or conflicting evidence is stated explicitly.

The product organizes research; it does not provide investment advice or execute trades.

## 2. Target user and release model

The primary user is an individual investor or researcher who collects company reports, articles, and notes over time.

V1 has:

- one private owner workspace;
- one isolated, sanitized, read-only demo workspace;
- no public registration, teams, billing, or organization management.

All V1 interface text, product messages, demo content, questions, and supported archive documents are English. Document language remains explicit metadata, and interfaces must not assume that English is the only possible future language.

Workspace isolation is required from the first release. The demo must contain no private archive data.

`Private workspace` describes who may access the workspace; it does not authorize external processing of confidential or sensitive content. External-provider eligibility follows the data boundaries in [Section 9](#9-data-and-retention-boundaries) and [04-security-and-operations.md](./04-security-and-operations.md#2-data-classification-and-external-provider-policy).

## 3. First complete user journey

1. Upload one or more PDFs or submit pasted text.
2. Preserve the original and show processing status.
3. Parse, chunk, and index the document.
4. Browse, filter, and search the archive.
5. Ask an archive question and receive a streamed answer.
6. Inspect citations at the original page and passage.
7. Receive an explicit notice when evidence is missing or conflicting.
8. Reprocess, soft-delete, or restore a document without stale content appearing in new answers.

This end-to-end journey is the primary V1 acceptance test.

## 4. V1 capabilities

### Access and companies

- Authenticate the private owner and isolate all workspace data.
- Create basic companies with names, aliases, and exchange-specific tickers.
- Maintain a watchlist.
- Assign documents to companies manually or through reviewable suggestions.

### Document capture and processing

- Import multiple PDFs and pasted text.
- Preserve immutable originals and version edited text.
- Store document type, publication date, availability date, and import time.
- Detect exact duplicates without losing provenance.
- Extract page-aware text, headings, paragraphs, lists, and tables where supported.
- Create structure-aware chunks and searchable indexes.
- Show processing failures and allow safe retries and reprocessing.
- Soft-delete and restore documents during the recovery period.

Optional AI enrichment must not block a successfully parsed document from becoming searchable.

### Archive search

- Browse and filter by company, ticker, document type, date, and processing state.
- Find exact names, tickers, phrases, and semantically related passages.
- Combine lexical and semantic retrieval for research questions.
- Open results in their original document context.

### Research and chat

- Ask archive questions and contextual follow-up questions.
- Apply company, document, workspace, and time filters before retrieval.
- Distinguish document lookup from open-ended research questions.
- Stream answers and show partial or insufficient coverage.
- Preserve conflicting positions instead of silently choosing one as true.

### Answers and evidence

- Build answers from structured statements with validated evidence references.
- Cite the immutable document version, page, and passage where available.
- Keep conversation memory as context, never as evidence.
- Record used documents and chunks, search route, model and prompt version, latency, token usage, and estimated cost.
- Keep archive evidence separate from any future web evidence or general model knowledge.

### Operational visibility

- Show import and reprocessing status.
- Make failed background jobs visible and retryable.
- Trace processing, retrieval, reranking, generation, and validation.
- Support tested backup, restore, deletion, and search-index rebuild procedures.

## 5. Answer rules

These are enforced product rules, not optional prompt instructions:

1. Every archive-derived statement requires validated evidence.
2. The answer explicitly reports missing or insufficient archive evidence.
3. General model knowledge may not silently fill gaps.
4. Conflicting evidence is presented with citations for each position.
5. Publication, availability, and archive-import dates remain distinct.
6. Historical questions specify either public-knowledge time or personal-archive time.
7. Bullish, bearish, credibility, importance, and impact labels are research annotations, not objective truth or investment advice.

## 6. Core V1 user cases

| ID | User request | Expected result | Critical boundary |
|---|---|---|---|
| UC-01 | Import a PDF or text | Original preserved; progress visible; page-aware content searchable | Partial failures are visible and retryable |
| UC-02 | “Show Nvidia documents from Q1 2025” | Complete filtered list with metadata and links to originals | Publication and import dates are not interchangeable |
| UC-03 | “What risks to data-center growth appear in my Nvidia documents?” | Evidence-grouped archive answer with exact citations | No unsupported general knowledge |
| UC-04 | “Which of those risks was mentioned most recently?” | Follow-up retains explicit company and time context | Conversation memory is not evidence |
| UC-05 | “What was publicly knowable on 30 June 2025?” | Only evidence available by the selected cutoff is eligible | Public time and personal archive time are separate modes |
| UC-06 | Ask an unsupported question | Explicit insufficient-evidence response; cited partial findings if present | Absence in the archive does not mean false in reality |
| UC-07 | Open a citation | Correct immutable document version opens at the cited location | Citation must match the version used for generation |
| UC-08 | Reprocess or delete a document | Versioned outputs or immediate retrieval exclusion | Stale vector records cannot qualify as evidence |

## 7. Planned after V1

The next increment adds structured investment knowledge while reusing the V1 evidence, versioning, workspace, and temporal rules:

- people, organizations, aliases, roles, and company relationships;
- claims classified as fact, forecast, opinion, or recommendation;
- metrics, units, currencies, and reporting periods;
- support, contradiction, update, and supersession relationships;
- forecast outcomes and separate credibility assessments for facts and forecasts;
- topics and company–topic relationships;
- versioned company profiles with evidence-backed facts;
- watchlist impact suggestions with direction, mechanism, horizon, confidence, and evidence;
- personalized ranking, coverage gaps, and missing counterpositions.

Later extensions may include URL and social-thread capture, web search as a separate evidence domain, translation, saved questions, exports, mobile capture, teams, controlled LangGraph workflows, and approved external tools.

Advanced retrieval methods are adopted only when evaluation shows a material improvement.

## 8. Explicit non-goals for V1

- Brokerage integration, trade execution, portfolio accounting, or tax reporting.
- Live market data, price prediction, or automated buy/sell recommendations.
- Treating model output as confirmed truth.
- Arbitrary web ingestion or unrestricted autonomous agents.
- Public SaaS onboarding, teams, or billing.
- Sending private or sensitive documents to external AI providers before their data flows, retention, region, and deletion behavior are approved.

## 9. Data and retention boundaries

- V1 processes only public or non-sensitive documents through approved external providers.
- Demo content must be synthetic, openly licensed, or otherwise redistributable.
- Soft deletion removes a document from normal use immediately.
- Documents remain recoverable for 30 days, then enter irreversible deletion.
- Affected backups expire within 35 days unless a documented hold applies.

## 10. V1 release criteria

V1 is releasable when:

1. English PDFs and pasted text complete the full user journey.
2. A representative 50-page text PDF meets the provisional processing objective.
3. Exact, lexical, semantic, and hybrid search return useful results.
4. Every archive-derived answer statement passes server-side evidence validation.
5. Citations open the correct original version and location.
6. Missing, conflicting, and historical-evidence cases behave as defined.
7. Duplicate import, provider failure, retry, reprocessing, deletion, and stale-index tests preserve canonical state.
8. Private and demo workspace data cannot cross boundaries.
9. Imports and answers are diagnosable through audit records and AI traces.
10. Database restore and search-index rebuild procedures have been exercised.
11. Expected portfolio usage remains within the provisional combined cloud and AI budget of **EUR 100 per month**.

## 11. Scope rule

A feature enters V1 only if it is required for the complete user journey, closes a release-blocking production risk, or replaces work of comparable size. Other ideas move to a later increment.

Architecturally significant changes must also update the System Design and, when they change an important decision, receive an ADR.
