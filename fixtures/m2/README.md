# M2 synthetic lifecycle contract fixtures

[`lifecycle-cases.json`](./lifecycle-cases.json) is a test specification for the accepted
[M2 contracts](../../docs/15-m2-contracts.md), not a retrieval-quality benchmark or a passing-test
report. All company names, amounts, and source text in this catalog are fictional and freely
reusable for project tests. The two canonical text originals are embedded as `original_text`.
IDs are fixture aliases, not production UUIDs, vector IDs, or authorization tokens.
`contract_accepted_on` records the contract decision; `expected_outcomes_executed = false` makes
clear that structural validation is not execution of the ten runtime scenarios.

Version 1 reports USD 12 million; the corrected version reports USD 14 million. The old revenue
sentence is the known historical citation. Reusing version-1 bytes with a new request key models an
intentional exact duplicate; reusing the same key models replay. `evidence_text` occurs exactly
once in its original, allowing later tests to derive immutable code-point ranges and SHA-256 from
the actual UTF-8 bytes without storing placeholder hashes or artificial database identities.
The supplementary-plane globe before the evidence distinguishes code-point offsets from UTF-16
code units; a later test must not use an ordinary JavaScript string index as the canonical offset.

The batch scenario reuses the existing public-synthetic
[Northstar PDF](../m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf) as two separate
file parts with distinct item/request keys. Its invalid sibling is generated in the consuming test
as non-PDF bytes. No new binary PDF or live provider resource is needed. Historical PDF access
also reuses that original and its existing source anchors; the new two-version fixture is plain text.

## Consuming boundaries

- Do not automatically import this catalog into the application, normal Compose runtime, M1/M1.5
  smoke tests, evaluation reports, or the owner's archive.
- Later offline tests create trusted workspace/run/attempt IDs and deterministic vector IDs through
  application contracts, using injectable clocks and provider/queue/storage doubles.
- PostgreSQL/Compose integration tests use isolated disposable resources. Failure injection must
  never stop or clear the owner's normal Redis, storage, Pinecone namespace, or database.
- Partial visibility, stale vectors, lost tasks, and interrupted purge are injected conditions,
  not claims that these scenarios were executed by creating this file.
- A live Pinecone absence test remains separate, opt-in, bounded, and explicitly approved.
- Keep this lifecycle corpus separate from M1.5's question/answer quality scores and provider-spend
  observations. No question-quality score is implied here.

Step 0 verification checks JSON structure, unique fixture/case IDs, the two version numbers,
canonical UTF-8/newline behavior, exact evidence occurrence, and source references. Runtime tests
and their required CI checks belong to the owning M2 implementation step. No permanent new fixture
runner command is claimed until one is implemented and verified.
