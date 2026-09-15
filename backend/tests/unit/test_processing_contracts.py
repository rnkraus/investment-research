"""Unit tests for bounded, application-owned document parser contracts."""

from io import BytesIO
from typing import BinaryIO
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.processing.contracts import (
    MAX_CONFIGURABLE_EXTRACTED_CHARACTERS,
    MAX_CONFIGURABLE_PDF_PAGES,
    ParsedDocument,
    ParsedPage,
    ParserFailureV1,
    ParserLimits,
    ParserRequestV1,
    ParserResponseV1,
    ProcessingJobV1,
    parser_response_byte_limit,
    processing_job_idempotency_key,
)
from investment_research.modules.processing.ports import (
    DocumentParser,
    ProcessingDispatcher,
)

WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
PROCESSING_RUN_ID = UUID("10000000-0000-4000-8000-000000000004")


class FakeDocumentParser:
    """Minimal structural implementation used to verify the parser port."""

    @property
    def parser_version(self) -> str:
        """Identify the deterministic fake implementation."""

        return "fake-parser-1"

    def parse(self, content: BinaryIO, *, limits: ParserLimits) -> ParsedDocument:
        """Return fixed page text after observing the caller-owned inputs."""

        assert content.read() == b"%PDF-synthetic"
        assert limits.max_pages == 10
        return ParsedDocument(pages=(ParsedPage(page_number=1, text="Revenue grew."),))


class RecordingProcessingDispatcher:
    """Record typed jobs without depending on Celery or Redis."""

    def __init__(self) -> None:
        self.jobs: list[ProcessingJobV1] = []

    def dispatch(self, job: ProcessingJobV1) -> None:
        """Record one already validated processing job."""

        self.jobs.append(job)


def test_processing_job_has_stable_minimal_json_identity() -> None:
    job = ProcessingJobV1.create(
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
    )

    expected_key = f"processing-job.v1:{WORKSPACE_ID}:{PROCESSING_RUN_ID}"
    assert job.idempotency_key == expected_key
    assert processing_job_idempotency_key(WORKSPACE_ID, PROCESSING_RUN_ID) == expected_key
    assert job.model_dump(mode="json") == {
        "schema_version": "processing-job.v1",
        "workspace_id": str(WORKSPACE_ID),
        "processing_run_id": str(PROCESSING_RUN_ID),
        "idempotency_key": expected_key,
    }


@pytest.mark.parametrize(
    ("override", "expected_error"),
    [
        pytest.param(
            {"schema_version": "processing-job.v2"},
            "literal_error",
            id="unknown-schema-version",
        ),
        pytest.param(
            {"idempotency_key": "processing-job.v1:forged"},
            "value_error",
            id="mismatched-idempotency-key",
        ),
        pytest.param(
            {"document_content": "%PDF-untrusted"},
            "extra_forbidden",
            id="unexpected-document-content",
        ),
    ],
)
def test_processing_job_rejects_untrusted_payload_changes(
    override: dict[str, object],
    expected_error: str,
) -> None:
    payload: dict[str, object] = {
        "schema_version": "processing-job.v1",
        "workspace_id": str(WORKSPACE_ID),
        "processing_run_id": str(PROCESSING_RUN_ID),
        "idempotency_key": processing_job_idempotency_key(
            WORKSPACE_ID,
            PROCESSING_RUN_ID,
        ),
    }
    payload.update(override)

    with pytest.raises(ValidationError) as error:
        ProcessingJobV1.model_validate(payload)

    assert expected_error in {item["type"] for item in error.value.errors()}


def test_processing_dispatcher_port_accepts_an_application_owned_implementation() -> None:
    dispatcher: ProcessingDispatcher = RecordingProcessingDispatcher()
    job = ProcessingJobV1.create(
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
    )

    dispatcher.dispatch(job)

    assert isinstance(dispatcher, RecordingProcessingDispatcher)
    assert dispatcher.jobs == [job]


def test_parser_limits_accept_positive_bounded_values() -> None:
    limits = ParserLimits(
        max_pages=25,
        max_extracted_characters=100_000,
    )

    assert limits.max_pages == 25
    assert limits.max_extracted_characters == 100_000


@pytest.mark.parametrize(
    ("field", "value", "expected_error_type"),
    [
        pytest.param("max_pages", 0, "greater_than_equal", id="zero-pages"),
        pytest.param(
            "max_pages",
            MAX_CONFIGURABLE_PDF_PAGES + 1,
            "less_than_equal",
            id="too-many-pages",
        ),
        pytest.param(
            "max_extracted_characters",
            0,
            "greater_than_equal",
            id="zero-characters",
        ),
        pytest.param(
            "max_extracted_characters",
            MAX_CONFIGURABLE_EXTRACTED_CHARACTERS + 1,
            "less_than_equal",
            id="too-many-characters",
        ),
    ],
)
def test_parser_limits_reject_out_of_range_values(
    field: str,
    value: int,
    expected_error_type: str,
) -> None:
    payload = {
        "max_pages": 25,
        "max_extracted_characters": 100_000,
        field: value,
    }

    with pytest.raises(ValidationError) as error:
        ParserLimits.model_validate(payload)

    assert error.value.errors()[0]["type"] == expected_error_type


def test_parsed_document_preserves_ordered_physical_pages() -> None:
    document = ParsedDocument(
        pages=(
            ParsedPage(page_number=1, text="First page."),
            ParsedPage(page_number=2, text=""),
            ParsedPage(page_number=3, text="Third page."),
        )
    )

    assert document.page_count == 3
    assert document.extracted_character_count == len("First page.Third page.")
    assert document.pages[1].text == ""
    assert set(document.model_dump()) == {"pages"}


@pytest.mark.parametrize(
    "pages",
    [
        pytest.param((), id="no-pages"),
        pytest.param(
            (
                ParsedPage(page_number=1, text="First"),
                ParsedPage(page_number=3, text="Third"),
            ),
            id="page-number-gap",
        ),
        pytest.param(
            (
                ParsedPage(page_number=2, text="Second"),
                ParsedPage(page_number=1, text="First"),
            ),
            id="out-of-order",
        ),
    ],
)
def test_parsed_document_rejects_invalid_page_sequences(
    pages: tuple[ParsedPage, ...],
) -> None:
    with pytest.raises(ValidationError):
        ParsedDocument(pages=pages)


def test_parser_contracts_are_frozen_and_reject_unknown_fields() -> None:
    page = ParsedPage(page_number=1, text="Exact text")

    with pytest.raises(ValidationError) as frozen_error:
        page.text = "Changed text"
    with pytest.raises(ValidationError) as extra_error:
        ParsedPage.model_validate({"page_number": 1, "text": "Exact text", "unexpected": True})

    assert frozen_error.value.errors()[0]["type"] == "frozen_instance"
    assert extra_error.value.errors()[0]["type"] == "extra_forbidden"


def test_document_parser_port_accepts_an_application_owned_implementation() -> None:
    parser: DocumentParser = FakeDocumentParser()
    content = BytesIO(b"%PDF-synthetic")
    limits = ParserLimits(max_pages=10, max_extracted_characters=1_000)

    result = parser.parse(content, limits=limits)

    assert parser.parser_version == "fake-parser-1"
    assert result.pages == (ParsedPage(page_number=1, text="Revenue grew."),)
    assert content.closed is False


def test_parser_protocol_round_trips_bounded_metadata_and_document() -> None:
    limits = ParserLimits(max_pages=10, max_extracted_characters=1_000)
    request = ParserRequestV1(
        parser_version="fake-parser-1",
        parser_config_version="fake-parser-config-1",
        limits=limits,
    )
    response = ParserResponseV1(
        parser_version=request.parser_version,
        parser_config_version=request.parser_config_version,
        document=ParsedDocument(pages=(ParsedPage(page_number=1, text="Revenue grew."),)),
    )

    assert ParserRequestV1.model_validate_json(request.model_dump_json()) == request
    assert ParserResponseV1.model_validate_json(response.model_dump_json()) == response
    assert request.model_dump(mode="json")["schema_version"] == "parser-protocol.v1"
    assert response.model_dump(mode="json")["schema_version"] == "parser-protocol.v1"


def test_parser_protocol_rejects_unknown_version_and_fields() -> None:
    with pytest.raises(ValidationError):
        ParserRequestV1.model_validate(
            {
                "schema_version": "parser-protocol.v2",
                "parser_version": "fake-parser-1",
                "parser_config_version": "fake-parser-config-1",
                "limits": {"max_pages": 10, "max_extracted_characters": 1_000},
            }
        )

    with pytest.raises(ValidationError):
        ParserFailureV1.model_validate(
            {
                "code": "parse_failed",
                "implementation_details": "must not cross the boundary",
            }
        )


def test_parser_response_limit_accounts_for_json_expansion_and_pages() -> None:
    limits = ParserLimits(max_pages=10, max_extracted_characters=1_000)

    assert parser_response_byte_limit(limits) == 64 * 1024 + 6_000 + 1_280
