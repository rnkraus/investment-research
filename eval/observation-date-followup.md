# Observation-date follow-up

On 2026-11-18, the original U03 question was submitted once through the rebuilt local API:
"What exact operating ARR did IREN report for November 6, 2026?"

The [preceding evaluation](rag-archive-current-run.md) accepted a PARTIAL proposal quoting an
earlier report's $1 billion operating ARR "today." The quote was exact but did not establish
the requested observation date.

## Change and observed result

- Source-ID answer prompt v12 requires INSUFFICIENT for an unsupported single dated value.
  It permits supported requested subparts of a multi-part question with their own time scopes.
- The source-ID API passes recognized observation-date questions to contextual support checking.
  Document-support prompt v2 checks the requested material part and date link. Bounded question
  text participates in cost reservation and recorded-decision identity during canonical reload.
- The live request used Terra/Terra, the existing small embedding model, standard search, the
  same 18-document archive, and no query decomposition or reranking. The API code and configured
  RAG file matched the working tree. Original checksums and current versions were verified.
- The eight final context chunks, their order, and their exact text matched the preceding U03
  capture. This was a prompt/policy follow-up, not a retrieval or embedding change.
- The terminal event was `answer.insufficient`, coverage `INSUFFICIENT`, with zero statements
  and zero citations. The request made one query-embedding call and one answer-generation call.
  No support call was needed because the answerer abstained.
- Recorded usage-based estimated cost was USD 0.008623. Local elapsed request/inspection time
  was 8.828 seconds. Raw streams, source identifiers, and usage records remain ignored.

This is one successful repeat, not a reliability estimate. It did not live-test the revised
checker against the rejected historical proposal, rerun A09, or repeat the full catalog.
The historical unsuccessful result remains recorded.

## Deterministic verification

- Backend formatting, lint, and type checking passed; 2,876 unit tests passed.
- PostgreSQL verification passed schema checks and migration cycles, with 433 integration
  tests passed and seven separate object-storage tests skipped as expected.
- Frontend formatting, lint, type checking, generated-contract consistency, and 100 tests passed.
- Regression coverage checks date-question transport for Terra and Luna, question bounds,
  decision identity, provider accounting, final canonical revalidation, and preservation of
  multi-part observation/target instructions. Stub responses verify contracts, not live semantics.

No database migration, dependency, public API schema, context limit, or embedding-model change
was required. The expanded-corpus evaluation remains the next agreed quality measurement.
