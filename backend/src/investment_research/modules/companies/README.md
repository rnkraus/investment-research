# Companies

Own workspace-scoped economic company identities separately from tickers, documents, and evidence.

## Implemented foundation

| File | Role |
|---|---|
| `domain.py` | Immutable company identity, explicit workspace/creator provenance, UTC creation time, and bounded Unicode name normalization. |
| `contracts.py` | Name-only creation, safe summaries, and bounded list/query/continuation contracts. |
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

`CompanyService.list_companies` reads a literal name substring within the trusted workspace.
Search uses the stored NFC/whitespace/casefold rules, preserves accents and punctuation, and treats
SQL wildcard/escape characters literally. Blank search is absent; controls are rejected. Pages
default to 20, allow 1–100, and order by comparison key using PostgreSQL `C` collation, then UUID.
Filtering happens before the limit; one extra row determines whether another page exists.

The typed internal continuation contains only a version, company UUID, and query fingerprint.
The fingerprint binds trusted workspace, normalized search, page size, and fixed ordering. The
repository resolves the anchor in scope and requires it to match the filter; a changed query or
unknown/foreign anchor yields the same `InvalidCompanyCursorError`. Tokens are untrusted positions,
not authorization. Pagination is not a snapshot: later insertions before the current position
require restarting the list. Company names are currently immutable and company deletion is absent.
Transport encoding and bounded HTTP parsing belong to the next API block.

There is no HTTP route, document assignment, or UI yet. Capture, retrieval, citations, deletion,
and provider calls remain unchanged. Existing backend and PostgreSQL CI gates discover the unit,
concurrency, workspace-isolation, transaction, pagination, and migration tests automatically.
