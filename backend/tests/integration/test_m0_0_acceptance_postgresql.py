"""End-to-end acceptance proof for the complete deterministic M0.0 API journey."""

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

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
from investment_research.settings import ApiSettings, WorkerSettings

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = BACKEND_ROOT.parent
FIXTURE_PDF = (
    REPOSITORY_ROOT / "fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf"
)
UNANSWERABLE_QUESTION = "What dividend did Northstar Grid Analytics declare for FY2025?"
EXPECTED_PUBLICATION_TIME = "2026-02-19T00:00:00Z"
EXPECTED_CITATION_PAGE = 2
ISOLATED_RUNNER_CODE = """\
import sys

from investment_research.runner import main

raise SystemExit(main(sys.argv[1:], env_file=None))
"""


def parse_server_sent_events(body: str) -> list[dict[str, Any]]:
    """Decode a complete deterministic server-sent-event response."""

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


def process_captured_run(
    settings: WorkerSettings,
    processing_run_id: str,
) -> subprocess.CompletedProcess[str]:
    """Execute the same bounded separate runner presented by the web UI."""

    runner_environment = os.environ.copy()
    runner_environment.update(
        {
            "INVESTMENT_RESEARCH_DATABASE_URL": settings.require_database_url(),
            "INVESTMENT_RESEARCH_ENVIRONMENT": "test",
            "INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT": str(settings.object_storage_root),
            "INVESTMENT_RESEARCH_PROCESSING_TIMEOUT_SECONDS": str(
                settings.processing_timeout_seconds
            ),
            "INVESTMENT_RESEARCH_PROCESSING_MEMORY_LIMIT_BYTES": str(
                settings.processing_memory_limit_bytes
            ),
        }
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            ISOLATED_RUNNER_CODE,
            processing_run_id,
        ],
        cwd=BACKEND_ROOT,
        env=runner_environment,
        capture_output=True,
        text=True,
        timeout=settings.processing_timeout_seconds + 15,
        check=False,
    )


def test_complete_m0_0_journey_opens_exact_source_and_refuses_missing_evidence(
    integration_api_settings: ApiSettings,
    integration_worker_settings: WorkerSettings,
    tmp_path: Path,
) -> None:
    """Prove capture, processing, research, citation, and refusal as one journey."""

    storage_root = tmp_path / "objects"
    api_settings = integration_api_settings.model_copy(update={"object_storage_root": storage_root})
    worker_settings = integration_worker_settings.model_copy(
        update={"object_storage_root": storage_root}
    )
    application = create_app(api_settings)

    with TestClient(application) as client:
        with FIXTURE_PDF.open("rb") as fixture_file:
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

        runner = process_captured_run(worker_settings, str(capture.processing_run_id))
        assert runner.returncode == 0, runner.stderr
        assert runner.stderr == ""

        status_response = client.get(capture.processing_status_url)
        assert status_response.status_code == 200
        processing = ProcessingRunResult.model_validate(status_response.json())
        assert processing.processing_run_id == capture.processing_run_id
        assert processing.document_version_id == capture.document_version_id
        assert processing.state == "LEXICAL_READY"
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


def test_m0_0_api_rejects_pdf_above_the_configured_upload_limit(
    integration_api_settings: ApiSettings,
    tmp_path: Path,
) -> None:
    """Prove the multipart API rejects oversized content before publishing an object."""

    storage_root = tmp_path / "objects"
    settings = integration_api_settings.model_copy(
        update={
            "object_storage_root": storage_root,
            "max_upload_bytes": FIXTURE_PDF.stat().st_size - 1,
        }
    )
    application = create_app(settings)

    with TestClient(application) as client, FIXTURE_PDF.open("rb") as fixture_file:
        response = client.post(
            "/api/v1/documents",
            data={
                "title": "Oversized M0.0 acceptance fixture",
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

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert response.json()["error"]["message"] == ("uploaded PDF exceeds the configured size limit")
    assert not storage_root.exists()
