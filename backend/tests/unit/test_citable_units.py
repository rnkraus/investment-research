"""Offline correctness tests for bounded, exact, request-scoped citation units."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import StructureBlock, StructureBlockType
from investment_research.modules.research.citable_unit_contracts import (
    M1_5_CITABLE_UNIT_CHARACTER_LIMIT,
    M1_5_CITABLE_UNIT_LIMIT,
    M1_5_CITABLE_UNIT_POLICY_VERSION,
    CitableSourceUnit,
    CitableUnitCatalog,
    CitableUnitError,
    CitableUnitKey,
    CitableUnitLimitDetails,
    CitableUnitLimitError,
    CitableUnitSource,
)
from investment_research.modules.research.citable_units import (
    build_citable_unit_catalog,
    resolve_citable_unit_ids,
)
from investment_research.modules.research.contracts import CanonicalCandidateSource
from investment_research.modules.search.contracts import (
    M1_CONTEXT_CHARACTER_LIMIT,
    M1_CONTEXT_CHUNK_LIMIT,
)

CONTEXT = WorkspaceContext(user_id=UUID(int=1), workspace_id=UUID(int=2))
REQUEST_ID = UUID(int=3)


def make_source(
    text: str,
    *,
    ordinal: int = 1,
    workspace_id: UUID = CONTEXT.workspace_id,
    pdf: bool = False,
) -> CitableUnitSource:
    """Create matching synthetic snapshots without a database or provider."""

    kind = SourceLocatorKind.PDF_PAGE if pdf else SourceLocatorKind.TEXT_PASSAGE
    block = StructureBlock(
        id=UUID(int=ordinal * 10 + 1),
        workspace_id=workspace_id,
        processing_run_id=UUID(int=ordinal * 10 + 2),
        document_version_id=UUID(int=ordinal * 10 + 3),
        source_locator_kind=kind,
        page_number=4 if pdf else None,
        block_type=StructureBlockType.PARAGRAPH,
        ordinal=0,
        text=text,
        start_character=40,
        end_character=40 + len(text),
        created_at=datetime(2026, 10, 7, tzinfo=UTC),
    )
    return CitableUnitSource(
        block=block,
        source=CanonicalCandidateSource(
            retrieval_candidate_id=UUID(int=ordinal * 10 + 4),
            chunk_id=UUID(int=ordinal * 10 + 5),
            document_id=UUID(int=ordinal * 10 + 6),
            document_version_id=block.document_version_id,
            structure_block_id=block.id,
            source_locator_kind=kind,
            page_number=block.page_number,
            text=text,
        ),
    )


def make_catalog(*sources: CitableUnitSource, request_id: UUID = REQUEST_ID) -> CitableUnitCatalog:
    """Build through the public preparation boundary."""

    return build_citable_unit_catalog(
        context=CONTEXT,
        research_request_id=request_id,
        sources=sources,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("First sentence. Second sentence!", ("First sentence.", "Second sentence!")),
        ("Is it ready?! Yes.", ("Is it ready?!", "Yes.")),
        ('He said "Ready!" Next.', ('He said "Ready!"', "Next.")),
        ("Ready?” Next.", ("Ready?”", "Next.")),
        ("Ready!')]} Next.", ("Ready!')]}", "Next.")),
        ("Dr. Lee confirmed it.", ("Dr.", "Lee confirmed it.")),
        ("Budget: $3.5 million. Next.", ("Budget: $3.5 million.", "Next.")),
        ("First paragraph\n \t\nSecond paragraph", ("First paragraph", "Second paragraph")),
        (
            "PDF text wraps\ninside a sentence. Next.",
            ("PDF text wraps\ninside a sentence.", "Next."),
        ),
        ("An archi-\ntecture remains unchanged.", ("An archi-\ntecture remains unchanged.",)),
        ("  A short block without punctuation \t", ("A short block without punctuation",)),
        ("One.\n\n\nTwo.\n", ("One.", "Two.")),
        ("Version 1.2; hostname.example; toshift", ("Version 1.2; hostname.example; toshift",)),
    ],
)
def test_ranges_preserve_every_visible_character(text: str, expected: tuple[str, ...]) -> None:
    source = make_source(text)
    catalog = make_catalog(source)

    assert tuple(unit.text for unit in catalog.units) == expected
    assert "".join(
        character for unit in catalog.units for character in unit.text if not character.isspace()
    ) == "".join(character for character in text if not character.isspace())
    for unit in catalog.units:
        assert unit.text == text[unit.key.start_character : unit.key.end_character]
        assert unit.key.workspace_id == CONTEXT.workspace_id
        assert unit.key.document_version_id == source.block.document_version_id
        assert unit.key.structure_block_id == source.block.id


def test_repeated_text_selects_the_exact_occurrence_without_string_search() -> None:
    text = "The pilot budget is $3.5 million. The pilot budget is $3.5 million."
    catalog = make_catalog(make_source(text))
    first, second = catalog.units

    assert first.text == second.text
    assert first.source_unit_id != second.source_unit_id
    assert second.key.start_character == len(first.text) + 1
    references = resolve_citable_unit_ids(
        context=CONTEXT,
        research_request_id=REQUEST_ID,
        catalog=catalog,
        source_unit_ids=(second.source_unit_id, first.source_unit_id),
    )
    assert references[0].start_character == second.key.start_character
    assert references[1].start_character == 0
    assert references[0].exact_text == second.text


@pytest.mark.parametrize("pdf", [False, True])
def test_unicode_offsets_and_original_locators_are_preserved(pdf: bool) -> None:
    first = "Café e\u0301 😀."
    text = f"{first}\n\tMore evidence."
    source = make_source(text, pdf=pdf)
    catalog = make_catalog(source)

    assert catalog.units[0].key.end_character == len(first)
    assert catalog.units[1].key.start_character == len(first) + 2
    assert catalog.sources[0].block == source.block
    assert catalog.sources[0].source.page_number == (4 if pdf else None)
    assert catalog.sources[0].block.start_character == 40


@pytest.mark.parametrize(
    ("text", "lengths"),
    [
        ("x" * 800, (800,)),
        ("x" * 801, (800, 1)),
        ("x" * 799 + " y", (799, 1)),
        ("x" * 800 + " y", (800, 1)),
        ("😀" * 1601, (800, 800, 1)),
    ],
)
def test_long_ranges_use_the_exact_code_point_limit(text: str, lengths: tuple[int, ...]) -> None:
    catalog = make_catalog(make_source(text))

    assert tuple(len(unit.text) for unit in catalog.units) == lengths
    assert all(len(unit.text) <= M1_5_CITABLE_UNIT_CHARACTER_LIMIT for unit in catalog.units)


def test_same_catalog_has_stable_handles_and_new_request_changes_only_handles() -> None:
    source = make_source("One. Two.")
    original = make_catalog(source)
    replay = make_catalog(source)
    other_request = make_catalog(source, request_id=UUID(int=99))

    assert original == replay
    assert tuple(unit.key for unit in original.units) == tuple(
        unit.key for unit in other_request.units
    )
    assert {unit.source_unit_id for unit in original.units}.isdisjoint(
        unit.source_unit_id for unit in other_request.units
    )
    key = original.units[0].key
    identity = json.dumps(
        [
            str(CONTEXT.workspace_id),
            str(source.block.document_version_id),
            str(source.block.id),
            0,
            4,
            M1_5_CITABLE_UNIT_POLICY_VERSION,
        ],
        separators=(",", ":"),
    )
    assert key.request_scoped_id(REQUEST_ID) == uuid5(REQUEST_ID, identity)


def test_same_original_range_is_offered_once_with_first_candidate_provenance() -> None:
    first = make_source("One. Two.")
    other_lane = CitableUnitSource(
        block=first.block,
        source=CanonicalCandidateSource.model_validate(
            {**first.source.model_dump(), "retrieval_candidate_id": UUID(int=90)}
        ),
    )
    catalog = make_catalog(first, other_lane)

    assert catalog.sources == (first,)
    assert len(catalog.units) == 2
    assert all(
        unit.retrieval_candidate_id == first.source.retrieval_candidate_id for unit in catalog.units
    )


def test_equal_text_in_different_blocks_is_not_deduplicated() -> None:
    catalog = make_catalog(make_source("Repeated."), make_source("Repeated.", ordinal=2))

    assert len(catalog.units) == 2
    assert catalog.units[0].source_unit_id != catalog.units[1].source_unit_id


def test_catalog_accepts_empty_context_and_exact_unit_limit() -> None:
    empty = make_catalog()
    exact = make_catalog(make_source("A. " * M1_5_CITABLE_UNIT_LIMIT))

    assert empty.sources == ()
    assert empty.units == ()
    assert len(exact.units) == M1_5_CITABLE_UNIT_LIMIT


def test_catalog_overflow_does_not_return_a_partial_catalog() -> None:
    with pytest.raises(CitableUnitLimitError, match="unit limit") as failure:
        make_catalog(make_source("A. " * (M1_5_CITABLE_UNIT_LIMIT + 1)))
    assert failure.value.details == CitableUnitLimitDetails(
        kind="unit_count", observed=257, limit=256
    )


def test_catalog_accepts_exact_source_and_character_limits() -> None:
    size = M1_CONTEXT_CHARACTER_LIMIT // M1_CONTEXT_CHUNK_LIMIT
    catalog = make_catalog(
        *(make_source("x" * size, ordinal=index + 1) for index in range(M1_CONTEXT_CHUNK_LIMIT))
    )

    assert len(catalog.sources) == M1_CONTEXT_CHUNK_LIMIT
    assert sum(len(item.block.text) for item in catalog.sources) == M1_CONTEXT_CHARACTER_LIMIT


def test_catalog_rejects_source_and_character_overflow() -> None:
    with pytest.raises(CitableUnitLimitError, match="source limit") as failure:
        make_catalog(*(make_source("A.", ordinal=index + 1) for index in range(9)))
    assert failure.value.details == CitableUnitLimitDetails(
        kind="source_count", observed=9, limit=8
    )
    with pytest.raises(CitableUnitLimitError, match="character limit") as failure:
        make_catalog(make_source("x" * (M1_CONTEXT_CHARACTER_LIMIT + 1)))
    assert failure.value.details == CitableUnitLimitDetails(
        kind="context_characters", observed=24_001, limit=24_000
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "private-source-marker"},
        {"observed": "257"},
        {"observed": True},
        {"observed": 256},
        {"observed": 255},
        {"limit": 0},
        {"limit": "256"},
    ],
)
def test_limit_diagnostics_require_known_categories_and_exceeded_integer_bounds(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CitableUnitLimitDetails.model_validate(
            {"kind": "unit_count", "observed": 257, "limit": 256, **changes}
        )


def test_foreign_workspace_is_rejected_without_source_text_in_error() -> None:
    source = make_source("SOURCE_CONTENT_MUST_NOT_LEAK.", workspace_id=UUID(int=999))

    with pytest.raises(CitableUnitError, match="foreign source") as failure:
        make_catalog(source)
    assert "SOURCE_CONTENT_MUST_NOT_LEAK" not in str(failure.value)


@pytest.mark.parametrize(
    "change",
    [
        {"text": "Different source."},
        {"structure_block_id": UUID(int=98)},
        {"document_version_id": UUID(int=98)},
        {"source_locator_kind": SourceLocatorKind.PDF_PAGE, "page_number": 1},
    ],
)
def test_source_requires_independent_matching_block(change: dict[str, object]) -> None:
    source = make_source("Original source.")
    modified = CanonicalCandidateSource.model_validate({**source.source.model_dump(), **change})

    with pytest.raises(ValidationError, match="complete canonical block"):
        CitableUnitSource(source=modified, block=source.block)


def test_duplicate_block_with_conflicting_document_provenance_is_rejected() -> None:
    source = make_source("Original source.")
    modified = CitableUnitSource(
        block=source.block,
        source=CanonicalCandidateSource.model_validate(
            {**source.source.model_dump(), "document_id": UUID(int=99)}
        ),
    )

    with pytest.raises(CitableUnitError, match="inconsistent candidate provenance"):
        make_catalog(source, modified)


@pytest.mark.parametrize("kind", ["unknown", "other_request", "other_workspace", "mixed"])
def test_resolution_rejects_ids_outside_current_catalog(kind: str) -> None:
    source = make_source("Original source.")
    catalog = make_catalog(source)
    other = make_catalog(source, request_id=UUID(int=99))
    context = CONTEXT
    ids: tuple[UUID, ...] = (UUID(int=100),)
    if kind == "other_request":
        ids = (other.units[0].source_unit_id,)
    elif kind == "other_workspace":
        context = WorkspaceContext(user_id=UUID(int=1), workspace_id=UUID(int=99))
        ids = (catalog.units[0].source_unit_id,)
    elif kind == "mixed":
        ids = (catalog.units[0].source_unit_id, other.units[0].source_unit_id)

    with pytest.raises(CitableUnitError):
        resolve_citable_unit_ids(
            context=context, research_request_id=REQUEST_ID, catalog=catalog, source_unit_ids=ids
        )


def test_resolution_rejects_wrong_request_even_with_known_ids() -> None:
    catalog = make_catalog(make_source("Evidence."))

    with pytest.raises(CitableUnitError, match="current request scope"):
        resolve_citable_unit_ids(
            context=CONTEXT,
            research_request_id=UUID(int=99),
            catalog=catalog,
            source_unit_ids=(catalog.units[0].source_unit_id,),
        )


@pytest.mark.parametrize("count", [0, 3])
def test_resolution_preserves_statement_evidence_limit(count: int) -> None:
    catalog = make_catalog(make_source("One. Two. Three."))

    with pytest.raises(CitableUnitError, match="one or two"):
        resolve_citable_unit_ids(
            context=CONTEXT,
            research_request_id=REQUEST_ID,
            catalog=catalog,
            source_unit_ids=tuple(unit.source_unit_id for unit in catalog.units[:count]),
        )


def test_resolution_rejects_duplicate_ids_and_preserves_two_separate_blocks() -> None:
    catalog = make_catalog(make_source("One."), make_source("Two.", ordinal=2))
    ids = tuple(unit.source_unit_id for unit in catalog.units)
    references = resolve_citable_unit_ids(
        context=CONTEXT, research_request_id=REQUEST_ID, catalog=catalog, source_unit_ids=ids
    )

    assert tuple(reference.exact_text for reference in references) == ("One.", "Two.")
    assert references[0].retrieval_candidate_id != references[1].retrieval_candidate_id
    with pytest.raises(CitableUnitError, match="distinct"):
        resolve_citable_unit_ids(
            context=CONTEXT,
            research_request_id=REQUEST_ID,
            catalog=catalog,
            source_unit_ids=(ids[0], ids[0]),
        )


@pytest.mark.parametrize("mutation", ["text", "handle", "key", "missing", "duplicate", "order"])
def test_catalog_validation_rejects_tampered_units(mutation: str) -> None:
    catalog = make_catalog(make_source("One. Two."))
    payload = catalog.model_dump(mode="json")
    if mutation == "text":
        payload["units"][0]["text"] = "Bad."
    elif mutation == "handle":
        payload["units"][0]["source_unit_id"] = str(UUID(int=99))
    elif mutation == "key":
        payload["units"][0]["key"]["workspace_id"] = str(UUID(int=99))
    elif mutation == "missing":
        payload["units"].pop()
    elif mutation == "duplicate":
        payload["units"].append(payload["units"][0])
    else:
        payload["units"].reverse()

    with pytest.raises(ValidationError):
        CitableUnitCatalog.model_validate(payload)


def test_unit_contract_rejects_invalid_ranges_and_changed_policy() -> None:
    catalog = make_catalog(make_source("One."))
    unit = catalog.units[0]
    with pytest.raises(ValidationError):
        CitableUnitKey.model_validate({**unit.key.model_dump(), "end_character": 0})
    with pytest.raises(ValidationError):
        CitableUnitKey.model_validate({**unit.key.model_dump(), "start_character": True})
    with pytest.raises(ValidationError):
        CitableUnitKey.model_validate({**unit.key.model_dump(), "unit_policy_version": "unknown"})
    with pytest.raises(ValidationError):
        CitableSourceUnit.model_validate({**unit.model_dump(), "text": "One. "})


def test_contracts_are_immutable_and_hide_raw_source_in_repr() -> None:
    source = make_source("SOURCE_CONTENT_MUST_NOT_LEAK.")
    catalog = make_catalog(source)

    assert "SOURCE_CONTENT_MUST_NOT_LEAK" not in repr(catalog)
    assert "SOURCE_CONTENT_MUST_NOT_LEAK" not in repr(source)
    assert "SOURCE_CONTENT_MUST_NOT_LEAK" not in repr(catalog.units[0])
    with pytest.raises(ValidationError, match="frozen"):
        catalog.units[0].text = "Changed."


@pytest.mark.parametrize(
    "change",
    [
        {"workspace_id": UUID(int=999)},
        {"document_version_id": UUID(int=999)},
        {"structure_block_id": UUID(int=999)},
        {"start_character": 1},
        {"end_character": 3},
    ],
)
def test_handle_depends_on_every_canonical_range_component(change: dict[str, object]) -> None:
    original = make_catalog(make_source("One.")).units[0].key
    changed = CitableUnitKey.model_validate({**original.model_dump(), **change})

    assert changed.request_scoped_id(REQUEST_ID) != original.request_scoped_id(REQUEST_ID)


def test_catalog_rejects_overlapping_ranges_even_with_valid_handles_and_text() -> None:
    catalog = make_catalog(make_source("One. Two."))
    payload = catalog.model_dump(mode="json")
    key = CitableUnitKey.model_validate({**catalog.units[1].key.model_dump(), "start_character": 3})
    payload["units"][1].update(
        key=key.model_dump(mode="json"),
        source_unit_id=str(key.request_scoped_id(REQUEST_ID)),
        text=". Two.",
    )

    with pytest.raises(ValidationError, match="invalid ranges"):
        CitableUnitCatalog.model_validate(payload)


def test_catalog_rejects_unexplained_gap_even_with_valid_handles_and_text() -> None:
    catalog = make_catalog(make_source("One. Two."))
    payload = catalog.model_dump(mode="json")
    key = CitableUnitKey.model_validate({**catalog.units[1].key.model_dump(), "start_character": 6})
    payload["units"][1].update(
        key=key.model_dump(mode="json"),
        source_unit_id=str(key.request_scoped_id(REQUEST_ID)),
        text="wo.",
    )

    with pytest.raises(ValidationError, match="invalid ranges"):
        CitableUnitCatalog.model_validate(payload)
