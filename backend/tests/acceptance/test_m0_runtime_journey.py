"""Complete M0 fixture journey through the production-shaped processing runtime."""

import json
import os
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from investment_research.api.contracts import (
    DocumentAccepted,
    EvidenceSpanResult,
    ProcessingRunResult,
)
from investment_research.infrastructure.generation.deterministic import (
    M0_0_FIXTURE_ANSWER_CASE,
    M0_0_INSUFFICIENT_MESSAGE,
)
from investment_research.main import create_app
from investment_research.settings import load_api_settings

FIXTURE_PDF = Path("/verification-fixture.pdf")
RUNTIME_ACCEPTANCE_ENVIRONMENT = "INVESTMENT_RESEARCH_M0_RUNTIME_ACCEPTANCE"
EXPECTED_PUBLICATION_TIME = "2026-02-19T00:00:00Z"
EXPECTED_CITATION_PAGE = 2
UNANSWERABLE_QUESTION = "What dividend did Northstar Grid Analytics declare for FY2025?"
PROCESSING_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.25

pytestmark = pytest.mark.skipif(
    os.environ.get(RUNTIME_ACCEPTANCE_ENVIRONMENT) != "1",
    reason="isolated M0 runtime acceptance is not active",
)


def parse_server_sent_events(body: str) -> list[dict[str, Any]]:
    """Decode the complete deterministic server-sent-event response."""

    events: list[dict[str, Any]] = []
    for frame in body.strip().split("\n\n"):
        lines = frame.splitlines()
        events.append(
            {
                "event": lines[0].removeprefix("event: "),
                "data": json.loads(lines[1].removeprefix("data: ")),
            }
        )
    return events


def wait_for_terminal_processing(
    client: TestClient,
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
    """Prove automatic capture, processing, research, citation, and refusal."""

    assert FIXTURE_PDF.is_file()
    application = create_app(load_api_settings(env_file=None))

    with TestClient(application) as client, FIXTURE_PDF.open("rb") as fixture_file:
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

        processing = wait_for_terminal_processing(client, capture.processing_status_url)
        assert processing.processing_run_id == capture.processing_run_id
        assert processing.document_version_id == capture.document_version_id
        assert processing.state == "LEXICAL_READY", processing.error_summary
        assert processing.error_summary is None

        answer_response = client.post(
            "/api/v1/research-requests",
            json={
                "question": M0_0_FIXTURE_ANSWER_CASE.question,
                "context": {"document_ids": [str(capture.document_id)]},
            },
        )
        assert answer_response.status_code == 200
        answer_events = parse_server_sent_events(answer_response.text)
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
        assert evidence.page == EXPECTED_CITATION_PAGE
        assert evidence.exact_text == M0_0_FIXTURE_ANSWER_CASE.evidence_text
        assert evidence.text_sha256 == sha256(evidence.exact_text.encode()).hexdigest()
        assert evidence.viewer_url.endswith(f"#page={EXPECTED_CITATION_PAGE}")

        original_response = client.get(evidence.viewer_url.split("#", maxsplit=1)[0])
        assert original_response.status_code == 200
        assert original_response.headers["content-type"] == "application/pdf"
        assert original_response.content == FIXTURE_PDF.read_bytes()

        insufficient_response = client.post(
            "/api/v1/research-requests",
            json={
                "question": UNANSWERABLE_QUESTION,
                "context": {"document_ids": [str(capture.document_id)]},
            },
        )
        assert insufficient_response.status_code == 200
        insufficient_events = parse_server_sent_events(insufficient_response.text)
        assert [event["event"] for event in insufficient_events] == ["answer.insufficient"]
        assert insufficient_events[0]["data"]["coverage"] == "INSUFFICIENT"
        assert insufficient_events[0]["data"]["message"] == M0_0_INSUFFICIENT_MESSAGE
        assert "answer.statement" not in insufficient_response.text
        assert "answer.citation" not in insufficient_response.text
