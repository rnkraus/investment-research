# Documents

This module owns logical archive documents, immutable content versions, and immutable metadata for captured originals. M0.0 supports one active version and PDF originals; later milestones extend lifecycle behavior without changing these ownership boundaries.

## Contents

| File             | Responsibility                                                                                          |
| ---------------- | ------------------------------------------------------------------------------------------------------- |
| `contracts.py`   | Defines validated capture metadata and the stable application result.                                  |
| `application.py` | Validates and preserves a bounded PDF, then records its canonical metadata and processing intent.      |
| `domain.py`      | Defines validated document, version, artifact, lifecycle, media, hash, language, and storage values.   |
| `ports.py`       | Defines object storage, repository, binary-stream, and capture-unit-of-work contracts.                 |
| `persistence.py` | Maps the three PostgreSQL tables and implements scoped capture and eligible-read behavior.              |

## Entity ownership

```mermaid
flowchart LR
    D[Document<br/>logical item and lifecycle] -->|current version| DV[DocumentVersion<br/>immutable content edition]
    DV -->|one captured source| OA[OriginalArtifact<br/>immutable storage metadata]
```

- `Document` owns `document_type`, lifecycle state, and the current-version pointer.
- `DocumentVersion` owns version number, title, language, content checksum, and source times.
- `OriginalArtifact` owns the server-generated storage key, media type, byte count, and original checksum. Page-aware information is created later as versioned processing output rather than invented during capture.

## Capture transaction

```mermaid
flowchart TD
    INPUT[Untrusted PDF stream and metadata] --> TYPE[Validate filename, media type, and PDF header]
    TYPE --> LIMIT[Copy into bounded private spool and calculate SHA-256]
    LIMIT --> STORE[Atomically store under a server-generated workspace key]
    STORE --> BUILD[Build Document, DocumentVersion, OriginalArtifact, and CAPTURED ProcessingRun]
    BUILD --> UOW[CaptureUnitOfWork]
    UOW --> DOCS[DocumentRepository]
    UOW --> PROCESSING[ProcessingRepository]
    DOCS --> PG[(PostgreSQL transaction)]
    PROCESSING --> PG
    PG -->|commit succeeds| RESULT[Return stable capture identifiers]
    PG -->|commit fails| DISCARD[Discard only the uncommitted stored object]
```

The service accepts only a declared `application/pdf` with a `.pdf` filename and a supported `%PDF-…` signature. It enforces the configured byte limit before publishing an object, keeps at most 1 MiB of the validation spool in memory, calculates SHA-256 while copying, and verifies the storage adapter's returned key, size, and checksum.

`add_capture` rejects mismatched workspace or entity relationships before staging rows. It never commits. The capture unit of work binds both module repositories to one caller-owned SQLAlchemy session, so document metadata and the processing intent commit or roll back together. Because object storage cannot participate in the PostgreSQL transaction, a failed canonical transaction invokes the narrowly scoped `discard_uncommitted` compensation. It is not a general deletion path for committed originals.

## Database isolation and eligibility

Every table carries `workspace_id` and a direct foreign key to `workspaces`. Composite foreign keys additionally require related documents, versions, and originals to share that workspace. The deferred current-version constraint verifies that the selected version belongs to the same logical document while allowing both sides of the initial relationship to be inserted in one transaction.

Repository reads always filter by the trusted workspace. Version and original reads also require an `ACTIVE` document and the requested current version. Unknown, foreign, inactive, and mismatched identifiers therefore share the same `None` result.

Domain entities are frozen, and the repository exposes no update operation for `DocumentVersion` or `OriginalArtifact`. The reviewed Alembic migration adds PostgreSQL triggers that reject updates and deletes for both immutable tables, and the integration tests verify that protection directly against PostgreSQL.
