# Investment Research Archive — Future Feature Backlog

**Status:** Unscheduled candidates  
**Scope:** Product ideas that are not yet committed to a delivery milestone  
**Related:** [01-product-scope.md](./01-product-scope.md), [05-delivery-plan.md](./05-delivery-plan.md)

## 1. Purpose

This file preserves useful feature ideas without expanding V1 or silently committing them to a release.

An item moves into the Product Scope and Delivery Plan only after its user value, dependencies, security implications, evaluation method, and implementation cost have been reviewed.

## 2. Capture and document processing

### F-01 — Near-duplicate detection

Detect documents that contain substantially the same information with small edits, formatting differences, shortened passages, or revised wording.

The system should show similarity and provenance rather than automatically deleting either version.

### F-02 — Time-zone normalization

Normalize source timestamps to a canonical time zone while retaining the original timestamp, original time-zone information, and parsing confidence.

Unknown time zones must remain unknown rather than being guessed silently.

### F-03 — Fuzzy actor resolution

Detect potentially identical people or organizations based on similar names, handles, aliases, and affiliations.

Ask the user to confirm a merge when identity is uncertain. Merging must be reversible and preserve the original labels and provenance.

### F-04 — Dedicated notes and opinions

Provide a first-class editor for personal notes and opinions instead of treating every note as generic pasted text.

Personal notes should be clearly distinguishable from external sources and may reference documents, companies, people, claims, and evidence spans.

### F-05 — Structured pasted-tweet parsing

Recognize copied tweets in supported standard layouts and propose:

- author name;
- handle;
- publication timestamp;
- cashtags and mentioned companies;
- tweet identifier or URL when present;
- quoted or replied-to content when unambiguous.

The original pasted text must remain preserved. Parsed metadata stays reviewable.

### F-06 — Save LLM output into the archive

Add an explicit action that stores a selected LLM answer or explanation as a new archive document.

Required behavior:

- the user deliberately presses a save action; saving is never automatic;
- the stored document is prominently labeled `AI_GENERATED`;
- provider, model, generation time, prompt or request reference, and originating conversation are recorded where available;
- citations and referenced archive evidence remain linked;
- unsupported model text is not promoted to confirmed archive knowledge;
- later retrieval and answers visibly distinguish AI-generated material from human-authored or primary-source material;
- ranking can exclude or downweight AI-generated documents to prevent feedback loops.

This feature must prevent circular evidence, where a generated answer later appears to confirm itself.

## 3. People, expertise, and assessments

### F-07 — Sector-level expertise

Assess a person's expertise for a sector independently from expertise for an individual company.

Example: a person may have strong semiconductor expertise but limited knowledge of one specific company.

### F-08 — Assessment rationale

Require or encourage a reason when assigning credibility, expertise, importance, or relationship assessments.

The rationale should retain author, time, and version history.

### F-09 — Fixed document-importance scale

Evaluate whether document importance should use a defined `1–5` scale. The scale requires written anchors so different ratings have consistent meaning.

### F-10 — Forecast-based credibility updates

Use resolved forecast outcomes to suggest changes to forecast credibility.

The system should not change a person's score silently. It should show the supporting forecast sample, scoring method, time horizon, uncertainty, and a reviewable proposed change.

### F-11 — Three-level stance model

Represent stance separately at:

1. general person level;
2. person–company level;
3. individual statement level.

Supported values may include bullish, bearish, neutral, mixed, and unknown. The exact vocabulary and confidence model must be defined before implementation.

## 4. Questions and research interaction

### F-12 — Questions in additional languages

Accept questions and archive documents beyond the initial English-only focus while preserving correct retrieval across English and every explicitly supported additional language.

Language detection, query translation, multilingual embeddings, reranking, and citation rendering require evaluation per supported language.

### F-13 — Clarifying questions

Ask the user for clarification when a question has materially different interpretations, such as an ambiguous company, ticker, time range, metric, or temporal mode.

Clarification should be preferred over silently selecting a high-impact assumption.

### F-14 — Complete structured-research filters

After actors and claims exist, add explicit filters for:

- person or organization;
- stance;
- claim type;
- forecast state;
- credibility or expertise range;
- topic and company relationship.

These filters should become acceptance-tested product behavior rather than incidental search metadata.

## 5. Answer presentation

### F-15 — List-first answer mode

Present evidence-backed result lists as the primary output for collection-style questions, with an optional summary as a secondary view.

### F-16 — Collapsible statement history

Show the newest supported state first and allow users to expand older, updated, or superseded statements with their original evidence.

### F-17 — Semantic change highlighting

Highlight what materially changed between an older and newer statement rather than showing only a textual diff.

The generated explanation must cite both versions and remain labeled as a model-assisted comparison.

### F-18 — Transparent filter relaxation

Offer a controlled option to relax overly narrow topical filters when no result is found.

Rules:

- never relax workspace, authorization, deletion, or evidence-validity constraints;
- never change the selected historical cutoff silently;
- show exactly which filter would change;
- require confirmation for changes that alter the meaning of the question;
- keep strict and relaxed result sets distinguishable.

## 6. Analysis and personalization

### F-19 — Stance dissonance highlighting

Highlight when an individual statement differs from the author's established stance.

Examples:

- a generally bearish author makes a bullish statement;
- a generally bullish author makes a bearish statement.

The system must compare the statement with the correct person–company stance and effective time, not only a timeless global label. The result is a research signal, not proof of inconsistency or dishonesty.

### F-20 — Named weighting modes

Evaluate three user-facing ranking modes:

- `neutral` — ranking independent of personal credibility preferences;
- `balanced` — limits dominance by one source, stance, or viewpoint;
- `curated` — applies the user's credibility, importance, expertise, and recency preferences.

The precise scoring rules require evaluation before becoming product behavior.

### F-21 — Fine-grained ranking controls

Allow users to adjust the influence of recency, credibility, importance, expertise, stance diversity, and other approved ranking factors.

Controls change ordering, never evidence eligibility or truth status.

### F-22 — Displaced-result explanation

Show which results moved down or disappeared because of personalized weighting and explain the main contributing factors.

Users should be able to compare the personalized result set with the neutral baseline.

### F-23 — Bullish-to-bearish source ratio

Show bullish, bearish, neutral, mixed, and unknown source counts for a selected company, topic, and time range.

Display absolute counts and coverage limitations. Do not present the ratio as a market prediction.

### F-24 — Stance timeline

Visualize how source and statement stance for a company changes over time.

The view should distinguish:

- source count from weighted score;
- publication time from archive-ingestion time;
- newly added sources from genuine changes in existing sources;
- missing or uneven archive coverage.

## 7. Admission rule

Before scheduling a feature, define:

1. the concrete user case and expected output;
2. required source data and review workflow;
3. evidence, provenance, and temporal behavior;
4. security, privacy, feedback-loop, and misuse risks;
5. deterministic acceptance tests;
6. quality evaluation where model judgment is involved;
7. dependencies on the current delivery milestones.

Features that affect ranking, credibility, stance, or AI-generated archive content must remain explainable and reversible.
