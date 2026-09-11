# Search Infrastructure

This package contains technical adapters that implement application-owned retrieval ports when one
query must read physical data owned by several business modules.

## Contents

| File            | Responsibility                                                                    |
| --------------- | --------------------------------------------------------------------------------- |
| `__init__.py`   | Identifies the search-adapter package without exporting a concrete implementation. |
| `postgresql.py` | Implements `LexicalSearch` as one bounded, eligibility-first PostgreSQL query.      |

## Dependency direction

```mermaid
flowchart LR
    APP[Search application] --> PORT[LexicalSearch port]
    PG[PostgresLexicalSearch] -. implements .-> PORT
    PG --> DOCS[Documents mappings]
    PG --> PROCESSING[Processing mappings]
    PG --> DB[(PostgreSQL)]
```

Business modules do not import another module's `persistence.py`. The outer infrastructure adapter
may join their physical read models because lexical retrieval needs canonical document and
processing eligibility in one SQL statement. The research-retrieval unit of work creates this
adapter with the same short-lived session as the Research and Retrieval repositories and exposes it
through the `LexicalSearch` port; application code does not depend on the concrete class.

## Eligibility-first query

The adapter joins each chunk to its chunk set, processing run, structure block, document version,
and document. Before ranking, it requires:

- the trusted workspace on the chunk;
- an active document whose `current_version_id` is the chunk's version;
- an active chunk set with the requested lexical configuration version;
- a `LEXICAL_READY` processing run with the same lexical configuration version;
- optional requested-document, public-availability, and archive-capture filters;
- a positive match against PostgreSQL's English full-text query.

Only eligible rows are ranked with `ts_rank_cd`. Results use descending score and ascending
`chunk_id` as the final deterministic tie-breaker, then apply the validated candidate limit. The
adapter returns immutable `LexicalSearchHit` values and never commits a transaction.
