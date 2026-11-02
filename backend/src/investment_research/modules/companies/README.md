# Companies

Own workspace-scoped economic company identities separately from tickers, documents, and evidence.

## Implemented foundation

| File | Role |
|---|---|
| `domain.py` | Immutable company identity, explicit workspace/creator provenance, UTC creation time, and bounded Unicode name normalization. |
| `contracts.py` | Name-only `CreateCompanyCommand` and safe `CompanySummary` without internal comparison keys. |

Names preserve case, accents, and punctuation after NFC and whitespace normalization. The display
limit is 200 code points; casefolded comparison keys can require up to 600. Control, format, and
surrogate characters are rejected. These keys support a later scoped uniqueness constraint; name
matching does not establish legal-entity equivalence or authorize cross-workspace access.

There is no company repository, migration, application service, HTTP route, or UI yet. The current
block does not modify capture, document metadata, retrieval, citations, deletion, or provider calls.
Existing backend verification discovers its unit tests and module-boundary checks automatically.
