"""Complete M0 fixture journey through the production-shaped processing runtime."""

import os
import time
from hashlib import sha256
from pathlib import Path

import httpx2
import pytest

from investment_research.api.contracts import (
    DocumentAccepted,
    EvidenceSpanResult,
    ProcessingRunResult,
)
from investment_research.infrastructure.generation.deterministic import (
    M0_0_FIXTURE_ANSWER_CASE,
    M0_0_INSUFFICIENT_MESSAGE,
)
from tests.acceptance.research_stream import parse_answer_events

FIXTURE_PDF = Path("/verification-fixture.pdf")
RUNTIME_ACCEPTANCE_ENVIRONMENT = "INVESTMENT_RESEARCH_M0_RUNTIME_ACCEPTANCE"
RUNTIME_WEB_ORIGIN = "http://web:3000"
EXPECTED_PUBLICATION_TIME = "2026-02-19T00:00:00Z"
EXPECTED_CITATION_PAGE = 2
UNANSWERABLE_QUESTION = "What dividend did Northstar Grid Analytics declare for FY2025?"
PROCESSING_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.25

pytestmark = pytest.mark.skipif(
    os.environ.get(RUNTIME_ACCEPTANCE_ENVIRONMENT) != "1",
    reason="isolated M0 runtime acceptance is not active",
)


def wait_for_terminal_processing(
    client: httpx2.Client,
    status_url: str,
) -> ProcessingRunResult:
    """Poll the public status contract until processing reaches a terminal state."""

    deadline = time.monotonic() + PROCESSING_TIMEOUT_SECONDS
    last_result: ProcessingRunResult | None = None
    while time.monotonic() < deadline:
        response = client.get(status_url)
        assert response.status_code == 200
        last_result = ProcessingRunResult.model_validate(response.json())
        if last_result.state in {"LEXICAL_READY", "FAILED"}:
            return last_result
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(
        "processing did not reach a terminal state before timeout; "
        f"last_state={None if last_result is None else last_result.state}"
    )


def test_complete_m0_runtime_journey_uses_automatic_processing() -> None:
    """Prove the complete browser-facing journey through the live container stack."""

    assert FIXTURE_PDF.is_file()

    with (
        httpx2.Client(base_url=RUNTIME_WEB_ORIGIN, timeout=10.0) as client,
        FIXTURE_PDF.open("rb") as fixture_file,
    ):
        page_response = client.get("/")
        assert page_response.status_code == 200
        assert "Investment Research Archive" in page_response.text

        capture_response = client.post(
            "/api/v1/documents",
            data={
                "title": "Northstar Grid Analytics Ltd. - FY2025 Earnings Brief",
                "language": "en",
                "published_at": EXPECTED_PUBLICATION_TIME,
            },
            files={
                "file": (
                    FIXTURE_PDF.name,
                    fixture_file,
                    "application/pdf",
                )
            },
        )
        assert capture_response.status_code == 202
        capture = DocumentAccepted.model_validate(capture_response.json())

        processing_started_at = time.monotonic()
        processing = wait_for_terminal_processing(client, capture.processing_status_url)
        processing_duration_seconds = time.monotonic() - processing_started_at
        assert processing.processing_run_id == capture.processing_run_id
        assert processing.document_version_id == capture.document_version_id
        assert processing.state == "LEXICAL_READY", processing.error_summary
        assert processing.error_summary is None
        print(
            f"Observed upload-to-LEXICAL_READY duration: {processing_duration_seconds:.2f}s",
            flush=True,
        )

        answer_request = {
            "question": M0_0_FIXTURE_ANSWER_CASE.question,
            "context": {"document_ids": [str(capture.document_id)]},
        }
        answer_response = client.post("/api/v1/research-requests", json=answer_request)
        assert answer_response.status_code == 200
        answer_events = parse_answer_events(
            answer_response.text, document_ids=[capture.document_id]
        )
        assert [event["event"] for event in answer_events] == [
            "answer.statement",
            "answer.citation",
            "answer.completed",
        ]
        assert answer_events[0]["data"]["text"] == M0_0_FIXTURE_ANSWER_CASE.statement
        assert answer_events[1]["data"]["relationship"] == "SUPPORTS"
        assert answer_events[2]["data"]["coverage"] == "COMPLETE"

        evidence_span_id = answer_events[1]["data"]["evidence_span_id"]
        evidence_response = client.get(f"/api/v1/evidence-spans/{evidence_span_id}")
        assert evidence_response.status_code == 200
        evidence = EvidenceSpanResult.model_validate(evidence_response.json())
        assert evidence.document_id == capture.document_id
        assert evidence.document_version_id == capture.document_version_id
        assert evidence.locator.kind == "PDF_PAGE"
        assert evidence.locator.page == EXPECTED_CITATION_PAGE
        assert evidence.exact_text == M0_0_FIXTURE_ANSWER_CASE.evidence_text
        assert evidence.text_sha256 == sha256(evidence.exact_text.encode()).hexdigest()
        assert evidence.viewer_url == f"/sources/{evidence_span_id}"

        original_response = client.get(
            f"/api/v1/documents/{capture.document_id}/versions/"
            f"{capture.document_version_id}/original"
        )
        assert original_response.status_code == 200
        assert original_response.headers["content-type"] == "application/pdf"
        assert original_response.content == FIXTURE_PDF.read_bytes()

        repeated_response = client.post("/api/v1/research-requests", json=answer_request)
        assert repeated_response.status_code == 200
        repeated_events = parse_answer_events(
            repeated_response.text, document_ids=[capture.document_id]
        )
        assert [event["event"] for event in repeated_events] == [
            "answer.statement",
            "answer.citation",
            "answer.completed",
        ]
        assert (
            repeated_events[0]["data"]["research_request_id"]
            != answer_events[0]["data"]["research_request_id"]
        )
        assert repeated_events[1]["data"]["evidence_span_id"] == evidence_span_id

        insufficient_response = client.post(
            "/api/v1/research-requests",
            json={
                "question": UNANSWERABLE_QUESTION,
                "context": {"document_ids": [str(capture.document_id)]},
            },
        )
        assert insufficient_response.status_code == 200
        insufficient_events = parse_answer_events(
            insufficient_response.text, document_ids=[capture.document_id]
        )
        assert [event["event"] for event in insufficient_events] == ["answer.insufficient"]
        assert insufficient_events[0]["data"]["coverage"] == "INSUFFICIENT"
        assert insufficient_events[0]["data"]["message"] == M0_0_INSUFFICIENT_MESSAGE
        assert "answer.statement" not in insufficient_response.text
        assert "answer.citation" not in insufficient_response.text
