# Companies

Own workspace-scoped company identities and optional display tickers.

## Implemented foundation

| File | Role |
|---|---|
| `domain.py` | Immutable company identity, explicit workspace/creator provenance, UTC creation time, and bounded Unicode name normalization. |
| `contracts.py` | Company creation, ticker replacement, public summaries, and bounded listing contracts. |
| `application.py` | Server-owned identity/provenance, commit-before-return creation, and scoped lookup/listing. |
| `ports.py` | Repository and transaction contracts with safe duplicate-name and uncertain-commit errors. |
| `persistence.py` | PostgreSQL mapping, workspace-scoped reads, trusted creator checks, and database-enforced name uniqueness. |

Names preserve case, accents, and punctuation after NFC and whitespace normalization. The display
limit is 200 code points; casefolded comparison keys can require up to 600. Control, format, and
surrogate characters are rejected. These keys enforce workspace-scoped uniqueness; name
matching does not establish legal-entity equivalence or authorize cross-workspace access.

Migration `0015_company_identity` creates an empty canonical store without inferring companies
from existing documents. Its downgrade permits an empty rollback and refuses to discard company
records. Reads always use the trusted workspace; staging checks both workspace and creator. The
repository neither commits nor flushes, so callers retain transaction control. Concurrent name
collisions are resolved by PostgreSQL's unique constraint. Stored display names and comparison
keys are checked for consistency when read.

`CompanyService` assigns identity, creator, workspace, and creation time from trusted server
inputs. Creation returns a summary only after the write transaction exits successfully; lookup
uses a separate non-committing read session and reports unknown/foreign identities uniformly.
The infrastructure adapter in `infrastructure/database/company_unit_of_work.py` translates only
the exact company-name unique constraint to `CompanyNameConflictError`. Other integrity failures
are not name conflicts. A connection failure during commit becomes `CompanyCommitUncertainError`;
there is no automatic retry or claimed success. Both safe errors suppress storage traceback context
when rendered normally; callers must not log the raw underlying exception or submitted name.

`CompanyService.list_companies` matches a literal substring of the name or ticker within the trusted workspace.
Search uses the stored NFC/whitespace/casefold rules, preserves accents and punctuation, and treats
SQL wildcard/escape characters literally. Blank search is absent; controls are rejected. Pages
default to 20, allow 1–100, and order by comparison key using PostgreSQL `C` collation, then UUID.
Filtering happens before the limit; one extra row determines whether another page exists.

The typed internal continuation contains only a version, company UUID, and query fingerprint.
The fingerprint binds trusted workspace, normalized search, page size, and fixed ordering. The
repository resolves the anchor in scope and requires it to match the filter; a changed query or
unknown/foreign anchor yields the same `InvalidCompanyCursorError`. Tokens are untrusted positions,
not authorization. Pagination is not a snapshot: later insertions before the current position
require restarting the list. Renames can also move a company to a different position.

## Local HTTP interface

| Method | Request | Success |
|---|---|---|
| `POST /api/v1/companies` | JSON with `name` and optional `ticker` | `201` with the committed company summary. |
| `GET /api/v1/companies` | Optional `q`, `page_size`, `cursor` | `200` with `items` and nullable `next_cursor`. |
| `PATCH /api/v1/companies/{company_id}` | Supply `name`, `ticker`, or both. An explicit `null` ticker clears it. | `200` with the updated company summary. |
| `PATCH /api/v1/companies/{company_id}/ticker` | JSON with `ticker`, where `null` clears it | `200` with the updated company summary. |

Migration `0018_company_ticker` leaves the ticker empty for existing companies. Symbols are stored
in uppercase and allow up to 20 ASCII letters, digits, dots, and hyphens. They start with a letter
or digit. Tickers can repeat and carry no exchange identity.

The API derives workspace and creator from the existing trusted local identity. It rejects extra
body/query fields, repeated scalar query parameters, and query parameters on creation. JSON bodies
are limited to 64 KiB before parsing, including streamed bodies. Continuations are canonical
unpadded base64url strings bounded to 4,096 characters. Reuse `next_cursor` with the same normalized
query and page size. It is an opaque position, not a credential or an authorization grant.

Responses and handled errors use `Cache-Control: private, no-store`. Duplicate names return
`409 CONFLICT`; malformed input or an incompatible cursor returns `422 INVALID_REQUEST`;
oversized bodies return `413 PAYLOAD_TOO_LARGE`. Storage and uncertain-commit errors return a safe
`500 INTERNAL_ERROR` without retrying creation or disclosing database details. Request logs use
route templates rather than submitted names, query strings, or cursors.

The document company editor supports name and ticker edits. Renaming keeps the company ID and
document links. A name conflict rolls back the entire edit. Existing backend and
PostgreSQL CI gates cover HTTP behavior, workspace isolation, pagination, and migrations.
