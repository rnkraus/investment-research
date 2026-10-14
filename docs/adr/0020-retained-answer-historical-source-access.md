# ADR-0020 — Historical source access for retained validated answers

**Status:** Accepted for M2 implementation — owner-approved contracts on 2026-10-14;
runtime source-access changes remain gated by implementation tests.

**Scope:** M2 read-only access to the exact immutable sources of retained accepted answers.
For the new historical view, supersedes only ADR-0009's current-version-only restriction.
Its normalization, Unicode offsets, immutable anchors, integrity checks, and ordinary
current-source endpoints remain unchanged. Acceptance does not itself change runtime access.

## Context and decision drivers

Historical answers retain exact source-span identities, but today's public original/evidence
access rejects inactive document versions. Version promotion would therefore break opening an old
answer's retained original. Removing current-version predicates globally would also admit old
versions where only current evidence is intended. The owner approved keeping old citations
inspectable without making them evidence for new answers or weakening permanent deletion.

## Options considered

1. Keep old citations stored but unopenable: simple, but prevents checking retained answer evidence.
2. Allow all inactive-version original reads: broader than required and easy to confuse with
   new-answer eligibility.
3. Add an answer-bound historical view with separate authorization: selected. It keeps
   current retrieval unchanged and exposes only evidence already linked to a retained accepted answer.

## Decision

Use separate answer-and-span-addressed metadata and original endpoints specified in
[M2 contracts](../15-m2-contracts.md#7-historical-evidence-access-and-permanent-deletion).
An opaque ID is a lookup key, not authorization. Require all of:

- trusted workspace equality across retained answer, validated statement, span, block, version,
  document, and original metadata;
- `STREAMED` answer and `VALIDATED` statement linked to that exact span;
- document lifecycle still `ACTIVE`, even if the cited version is no longer current;
- unchanged original checksum, exact text, locator, and code-point offsets.

Return the same unavailable result for unknown, foreign, unlinked, deletion-pending, or purged
resources. Use private/no-store metadata and verified original responses, never direct object URLs.
The frontend labels the immutable historical version and uses the authorized historical-original
route. Ordinary current-source endpoints remain unchanged. Do not offer arbitrary inactive-version
browsing, new historical retrieval, a conversation/history screen, or a new model call here.

Internal candidate processing is a different capability: a canonical run and current fence may
authorize processing a pending version. It never grants historical-answer or public-original access.

ADR-0019 remains dominant for deletion. Exclusion denies history immediately; purge removes complete
affected answers and source relationships, including multi-document answers. Recheck lifecycle
before preparing downloads. Already transferred bytes, user downloads, exports, and provider-held
copies cannot be retracted by this endpoint.

## Consequences and validation

Old accepted answers remain independently inspectable after version promotion and reprocessing,
without recomputing their citations against new parser output. This adds a separate reader and
viewer authorization path, which must not be reused by new retrieval. Retaining exact historical
blocks has a storage cost; permanent deletion still removes those blocks and affected answers.

Before activation, test both PDF and pasted text, retained inactive versions, changed chunk sets,
promotion/deletion races, foreign/unknown equivalence, unvalidated/unlinked spans, exact Unicode
ranges, original-integrity failure, and failure to access purged sources. Preserve the old endpoints'
inactive-version refusal tests. No additional public hosting or private-provider-data approval is given.

Reconsider only if a future reviewed historical archive product needs broader version access;
that is not silently included in this answer-bound exception.
