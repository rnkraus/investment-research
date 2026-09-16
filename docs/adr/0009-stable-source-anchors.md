# ADR-0009 — Stable PDF and pasted-text source anchors

**Status:** Accepted

**Date:** 2026-09-17

**Scope:** M1 original-text normalization, parsed block locations, evidence locators, and citation
viewer behavior

## Context

M0 stores immutable PDF originals and anchors every `StructureBlock` and `EvidenceSpan` to a
physical PDF page. M1 adds pasted English text as a second immutable original type. Pasted text has
no honest page number, while existing PDF page numbers and character offsets already have persisted
meaning and must remain valid.

Chunks, embeddings, active processing runs, and retrieval indexes may be replaced over time.
Citations must instead remain resolvable against the exact immutable document version and canonical
parsed block that were validated when the answer was created.

The source-location contract crosses parsing, PostgreSQL, evidence validation, the HTTP API, and the
frontend viewer. An implicit convention or nullable page number without a discriminator would let
clients confuse PDF and text locations.

## Decision drivers

- preserve every historical M0 PDF page number and offset without reinterpretation;
- represent pasted text without fake pages or provider-specific storage details;
- keep exact-span validation identical for both media types;
- make offset units, newline behavior, and range boundaries unambiguous across Python, PostgreSQL,
  JSON, and TypeScript;
- keep citations stable when chunks, embeddings, retrieval configurations, or active runs change;
- authorize every source view through the existing workspace boundary;
- prevent raw source text from being copied into URLs, logs, vector metadata, or identifiers.

## Options considered

1. **One discriminated locator union plus media-neutral block-relative evidence offsets.** Selected.
   It preserves the proven exact-span invariant and makes media-specific navigation exhaustive.
2. **Assign pasted text synthetic page numbers.** Rejected because page boundaries would be
   arbitrary, unstable, and misleading.
3. **Use line and column numbers for pasted text.** Rejected because newline and display wrapping
   make them less stable than offsets in the canonical original.
4. **Use browser text fragments containing the cited text.** Rejected because source text would
   enter URLs and history, matching is browser-dependent, and repeated text is ambiguous.
5. **Store only a chunk ID or vector ID.** Rejected because chunks and vectors are derived,
   replaceable retrieval artifacts rather than evidence.
6. **Use UTF-8 byte offsets.** Rejected for the application contract because exact-text validation
   and browser highlighting operate on decoded text. Byte size and checksums still describe the
   stored bytes.

## Decision

### Canonical pasted-text original

The text-capture boundary accepts one bounded JSON string. Before hashing or storage, the
application:

1. replaces every `CRLF` sequence with `LF`;
2. replaces every remaining `CR` with `LF`;
3. performs no trimming and no Unicode normalization;
4. encodes the result as strict UTF-8 without adding a byte-order mark.

Invalid Unicode that cannot be encoded as strict UTF-8 is rejected. The resulting bytes are the
immutable original with media type `text/plain`. When served over HTTP, its content type includes
`charset=utf-8`. `byte_size`, the original checksum, and the document version content checksum
describe exactly those stored bytes. A later edit, including a newline or Unicode change, creates a
new document version and original artifact.

The isolated text parser decodes those bytes as strict UTF-8 and rejects content that violates the
canonical newline rule. It never silently rewrites stored content.

### Offset convention

Every character offset is:

- zero-based;
- a half-open range `[start_character, end_character)`;
- measured in Unicode code points in the decoded canonical text, not UTF-8 bytes, grapheme
  clusters, or JavaScript UTF-16 code units.

Python slicing therefore expresses the canonical invariant directly. TypeScript code must use a
code-point-aware representation when it slices or highlights text. PostgreSQL `char_length` checks
remain aligned with the stored exact text. Tests must include non-ASCII and supplementary-plane
characters so an accidental UTF-16-code-unit interpretation fails visibly.

### Structure blocks

`StructureBlock` receives a required `source_locator_kind` discriminator:

- `PDF_PAGE`: `page_number` is non-null and remains the one-based physical PDF page.
  `start_character` and `end_character` remain offsets inside the parser's canonical text for that
  page. Blank physical pages continue to count.
- `TEXT_PASSAGE`: `page_number` is null. `start_character` and `end_character` are absolute offsets
  in the complete canonical pasted-text original, and
  `block.text == original_text[start_character:end_character]`.

Both kinds retain the existing ordered, non-empty source-range constraints. A database check
constraint enforces the discriminator/page combination.

### Evidence spans and locators

`EvidenceSpan.start_character` and `EvidenceSpan.end_character` remain block-relative for both
media types. The invariant remains:

```python
span.exact_text == structure_block.text[span.start_character:span.end_character]
```

Each evidence span also stores one immutable discriminated source locator:

```text
PdfPageLocator
  kind = "PDF_PAGE"
  page_number >= 1

TextPassageLocator
  kind = "TEXT_PASSAGE"
  source_start_character >= 0
  source_end_character > source_start_character
```

For `TEXT_PASSAGE`, the absolute source range is calculated and validated when the evidence span is
created:

```python
source_start_character = structure_block.start_character + span.start_character
source_end_character = structure_block.start_character + span.end_character
```

It must also satisfy:

```python
span.exact_text == original_text[source_start_character:source_end_character]
```

The evidence table therefore adds a required `source_locator_kind`, makes `page_number` nullable,
and adds nullable `source_start_character` and `source_end_character` columns. Database check
constraints enforce exactly one valid shape:

| Kind           | `page_number` | source start/end          |
| -------------- | ------------- | ------------------------- |
| `PDF_PAGE`     | required      | both null                 |
| `TEXT_PASSAGE` | null          | both required and ordered |

The existing block-relative range, exact-text length, hash, workspace, document-version, and
structure-block constraints remain in force. The source locator is an immutable citation snapshot,
not a replacement for canonical server-side validation.

### Migration compatibility

Migration `0004_m1_text_sources_and_anchors` backfills every existing structure block and evidence
span with `source_locator_kind = 'PDF_PAGE'`. It leaves every existing `page_number`,
`start_character`, `end_character`, exact text, hash, relationship, and identifier unchanged.
Only after the backfill succeeds does the migration make the discriminator non-null and install the
new shape constraints.

No migration derives new offsets for historical PDF evidence. A migration test records representative
M0 values before upgrade and proves that the same citation resolves to the same page and exact text
after upgrade.

### API and viewer contract

The evidence API retains the block-relative `start_character` and `end_character` fields and exposes
one required discriminated `locator`:

```json
{ "kind": "PDF_PAGE", "page": 4 }
```

or:

```json
{ "kind": "TEXT_PASSAGE", "start_character": 120, "end_character": 184 }
```

The frontend handles this union exhaustively. Citation links use the opaque evidence-span identity:

```text
/sources/{evidence_span_id}
```

They never put exact source text in a URL. The source page resolves the evidence span and original
through authorized API endpoints:

- `PDF_PAGE` opens the integrity-checked immutable PDF at the stored physical page;
- `TEXT_PASSAGE` decodes the integrity-checked immutable text, verifies that the absolute range
  equals `exact_text`, and highlights that passage without changing whitespace.

Unknown locator kinds, checksum mismatches, invalid ranges, foreign workspaces, inactive document
versions, and unavailable originals fail closed. The API never returns a direct object-storage URL.

### Versioning

- Changing pasted-text normalization or offset units requires a new document version for recaptured
  text and a new parser configuration version; existing originals and citations are not rewritten.
- Changing parsing or block boundaries creates a new processing run and structure blocks.
- Changing chunking creates a new chunk set and leaves structure blocks and evidence spans intact.
- Changing embeddings creates a new embedding configuration and vector identity and leaves canonical
  source anchors intact.
- Historical answers continue to reference their immutable evidence spans and blocks even when a
  newer run or chunk set becomes active.

## Consequences

PDF behavior remains understandable and backward-compatible, while pasted text gains exact,
page-free navigation. Evidence validation remains centered on immutable originals and canonical
blocks rather than retrieval artifacts.

The schema and API gain a discriminator and two nullable text-locator columns. This duplication is
intentional: it preserves the exact source navigation used by a historical answer without depending
on whichever processing run is active later.

Frontend highlighting must be code-point-aware rather than using ordinary JavaScript string indexes.
The source viewer must retrieve and verify the original before rendering a text citation, which is
more work than linking to raw text but avoids ambiguous matching and content-bearing URLs.

## Validation and reconsideration triggers

Before M1 is complete, tests must prove:

1. newline normalization is deterministic and the stored bytes, byte size, and checksums agree;
2. Unicode code-point offsets behave identically across domain validation, PostgreSQL, API fixtures,
   and frontend highlighting;
3. each locator kind accepts only its defined non-null fields;
4. parsed text blocks reproduce the exact canonical original ranges;
5. evidence spans reproduce both their block-local text and, for text, their absolute original
   passage;
6. historical M0 PDF evidence keeps the same identifiers, page, offsets, exact text, and hash after
   migration;
7. changing chunks or embeddings does not change an existing citation;
8. PDF and text source links resolve only inside the trusted workspace and against an eligible
   immutable version;
9. the viewer opens the expected physical PDF page or highlights the exact pasted-text passage;
10. repeated identical text is resolved by its stored range rather than by string search.

Reconsider the locator union only when another accepted original media type cannot be represented
without weakening these invariants. Add a new discriminator variant; do not reinterpret an existing
one.
