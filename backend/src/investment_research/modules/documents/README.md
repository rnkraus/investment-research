# Documents

This module owns logical archive documents, immutable content versions, and immutable metadata for captured originals. The canonical model accepts PDF and plain-text originals while preserving the same ownership boundaries for later lifecycle work.

## Contents

| File             | Responsibility                                                                                       |
| ---------------- | ---------------------------------------------------------------------------------------------------- |
| `contracts.py`   | Defines validated capture metadata, bounded catalog pages, and stable application results.           |
| `application.py` | Captures bounded PDFs and resolves verified current originals for downloads or processing.           |
| `listing.py`     | Lists a workspace's current document metadata through a short-lived read unit of work.               |
| `domain.py`      | Defines validated document, version, artifact, lifecycle, media, hash, language, and storage values. |
| `ports.py`       | Defines object storage, repository, binary-stream, and write/read unit-of-work contracts.            |
| `persistence.py` | Maps the three PostgreSQL tables and implements scoped capture and eligible-read behavior.           |

## Entity ownership

```mermaid
flowchart LR
    D[Document<br/>logical item and lifecycle] -->|current version| DV[DocumentVersion<br/>immutable content edition]
    DV -->|one captured source| OA[OriginalArtifact<br/>immutable storage metadata]
```

- `Document` owns `document_type`, lifecycle state, and the current-version pointer.
- `DocumentVersion` owns version number, title, language, content checksum, and source times.
- `OriginalArtifact` owns the server-generated storage key, media type, byte count, and original checksum. Source-location information is created later as versioned processing output rather than invented during capture.

## Capture transaction

```mermaid
flowchart TD
    PDF[Untrusted PDF stream and metadata] --> PDF_VALIDATE[Validate filename, media type, PDF header, and bytes]
    TEXT[Untrusted pasted-text JSON] --> TEXT_VALIDATE[Reject invalid content and normalize newlines]
    PDF_VALIDATE --> PREPARED[PreparedOriginal<br/>canonical bytes, media type, size, and SHA-256]
    TEXT_VALIDATE --> PREPARED
    PREPARED --> STORE[Atomically store under a server-generated workspace key]
    STORE --> BUILD[Build Document, DocumentVersion, OriginalArtifact, and CAPTURED ProcessingRun]
    BUILD --> UOW[CaptureUnitOfWork]
    UOW --> DOCS[DocumentRepository]
    UOW --> PROCESSING[ProcessingRepository]
    DOCS --> PG[(PostgreSQL transaction)]
    PROCESSING --> PG
    PG -->|commit succeeds| JOB[Build versioned ProcessingJobV1]
    JOB --> DISPATCH[ProcessingDispatcher]
    DISPATCH -->|publish succeeds| RESULT[Return stable capture identifiers]
    DISPATCH -->|broker failure| LOG[Record safe operational event]
    LOG --> CAPTURED[Return identifiers; run remains CAPTURED]
    PG -->|commit fails| DISCARD[Discard only the uncommitted stored object]
```

The PDF path accepts only a declared `application/pdf` with a `.pdf` filename and a supported
`%PDF-…` signature. The pasted-text path accepts a strict JSON body with at most 100,000 Unicode
code points, rejects NUL and content without a visible character, converts `CRLF` and remaining `CR`
to `LF`, performs no trimming or Unicode normalization, and encodes strict UTF-8 without a byte-order
mark. Both paths enforce the configured byte limit before publishing an object, calculate SHA-256
over the exact stored bytes, and verify the storage adapter's returned key, size, and checksum.

`add_capture` rejects mismatched workspace or entity relationships before staging rows. It never commits. The capture unit of work binds both module repositories to one caller-owned SQLAlchemy session, so document metadata and the processing intent commit or roll back together. Because object storage cannot participate in the PostgreSQL transaction, a failed canonical transaction invokes the narrowly scoped `discard_uncommitted` compensation. It is not a general deletion path for committed originals.

After the canonical transaction closes successfully, the service creates the strict versioned
`ProcessingJobV1` and calls the application-owned `ProcessingDispatcher`. An expected broker
publication failure records only the safe workspace and processing-run identifiers, returns the
committed capture result, and leaves the run visibly `CAPTURED` for recovery. Dispatch is outside the
compensation block, so neither a broker failure nor an unexpected post-commit error can delete an
already canonical original. The API container constructs the Celery adapter and provides its
`ProcessingDispatcher` to the capture service explicitly. Focused service tests use typed recording
dispatchers so they remain independent of a live broker.

## Original access

```mermaid
flowchart TD
    DOWNLOAD_INPUT[Download<br/>workspace, document, and version IDs] --> READ[DocumentReadUnitOfWork]
    PROCESSING_INPUT[Processing<br/>workspace and canonical version ID] --> READ
    READ --> REPOSITORY[Shared eligible-original query]
    REPOSITORY -->|unknown, foreign, inactive, or mismatched| NOTFOUND[One unavailable result]
    REPOSITORY -->|eligible current version| METADATA[Canonical artifact metadata]
    METADATA --> STORAGE[ObjectStorage.open]
    STORAGE --> SPOOL[Bounded temporary spool]
    SPOOL --> VERIFY[Verify byte count and SHA-256]
    VERIFY -->|match| DOWNLOAD[OriginalDownload stream]
    VERIFY -->|mismatch| INTEGRITY[Integrity failure; emit no PDF bytes]
```

`OpenOriginalService` performs the scoped database lookup before opening storage. It copies the
bounded original into a private spool and verifies its recorded size and SHA-256 before returning a
stream. At most 1 MiB remains in memory for larger originals; the standard-library spool moves the
remainder to a temporary file. The stream owns that temporary resource and closes it after normal,
failed, or cancelled consumption.

The download path requires both document and version identifiers. The internal processing path
starts from the canonical version identifier already recorded on its `ProcessingRun`. Both paths use
the same active-current-workspace eligibility query and byte-integrity preparation. Processing sees
only the `ProcessingOriginalProvider` stream contract, not document tables or storage paths.

The read unit of work opens one short-lived SQLAlchemy session and exposes only the document
repository. It does not reuse the capture transaction, include the processing repository, or commit
on a read-only request.

## Current-document catalog reads

```mermaid
flowchart LR
    INPUT[Trusted WorkspaceContext and DocumentListQuery] --> SERVICE[ListDocumentsService]
    SERVICE --> READ[DocumentReadUnitOfWork]
    READ --> REPOSITORY[DocumentRepository.list_current_documents]
    REPOSITORY --> JOIN[Active document and its current version and original metadata]
    JOIN --> ORDER[Creation time and document UUID descending]
    ORDER --> BOUND[Page size plus one lookahead row]
    BOUND --> PAGE[DocumentListPage and optional next cursor]
```

The owner approved a small read-only document-selection follow-up ahead of the broader M3 archive
work. This first backend block exposes application-owned metadata reads only: it does not yet add
an HTTP route, processing-status composition, or a browser list. Those are subsequent blocks.

`DocumentListQuery` defaults to 20 entries and accepts 1–100. Its optional `after` cursor contains
the last returned document's timezone-aware creation time and UUID. The next query uses an
exclusive descending keyset boundary, not an offset. Equal timestamps remain deterministic, and
newer imports do not shift already traversed page boundaries. This is not a frozen snapshot across
requests; refresh the first page to see new arrivals. The cursor is a position, never authorization.

Every join includes the workspace relationship. Only `ACTIVE` documents with their current version
and original metadata are returned. Entries include opaque document/version IDs, title, language,
media type, byte size, and separately named creation, capture, publication, and availability times.
Unknown source times stay null. Storage keys, checksums, original bytes, and provider data are not
selected or exposed. Listing original metadata does not replace integrity and eligibility checks
when the original is subsequently opened.

The service closes its read session before returning and performs no writes, storage access,
processing-table reads, or paid-provider calls. The existing schema is unchanged. Unit tests cover
query bounds, field allowlists, SQL scope and ordering, and read-session cleanup; PostgreSQL tests
cover workspace isolation, equal-time pagination with concurrent newer imports, missing originals,
and current-version selection. Broader M3 browsing and lifecycle gates remain separate.

## Database isolation and eligibility

Every table carries `workspace_id` and a direct foreign key to `workspaces`. Composite foreign keys additionally require related documents, versions, and originals to share that workspace. The deferred current-version constraint verifies that the selected version belongs to the same logical document while allowing both sides of the initial relationship to be inserted in one transaction.

Repository reads always filter by the trusted workspace. Version and original reads also require an `ACTIVE` document and the requested current version. Unknown, foreign, inactive, and mismatched identifiers therefore share the same `None` result.

Domain entities are frozen, and the repository exposes no update operation for `DocumentVersion` or `OriginalArtifact`. The reviewed Alembic migration adds PostgreSQL triggers that reject updates and deletes for both immutable tables, and the integration tests verify that protection directly against PostgreSQL.
