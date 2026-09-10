"""PostgreSQL integration test for the separate single-job runner process."""

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine

from investment_research.infrastructure.database.session import create_session_factory
from investment_research.infrastructure.database.unit_of_work import (
    create_capture_unit_of_work_factory,
)
from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.modules.documents.application import CaptureDocumentService
from investment_research.modules.documents.contracts import CaptureDocumentInput
from investment_research.modules.identity.application import FixedLocalIdentityContextProvider
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import SqlAlchemyProcessingRepository

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = BACKEND_ROOT.parent
FIXTURE_PDF = (
    REPOSITORY_ROOT / "fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf"
)
CAPTURED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)


def test_runner_processes_one_captured_run_and_exits(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    context = FixedLocalIdentityContextProvider().resolve()
    storage_root = tmp_path / "objects"
    session_factory = create_session_factory(integration_database_engine)
    capture_service = CaptureDocumentService(
        object_storage=LocalObjectStorage(storage_root),
        unit_of_work_factory=create_capture_unit_of_work_factory(session_factory),
        max_upload_bytes=FIXTURE_PDF.stat().st_size + 1,
        clock=lambda: CAPTURED_AT,
    )
    capture_input = CaptureDocumentInput(
        filename=FIXTURE_PDF.name,
        media_type="application/pdf",
        title="Runner Process Integration Fixture",
        language="en",
        published_at=CAPTURED_AT,
        available_at=CAPTURED_AT,
    )
    with FIXTURE_PDF.open("rb") as content:
        capture = capture_service.capture(context, capture_input, content)

    runner_environment = os.environ.copy()
    runner_environment.update(
        {
            "INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT": str(storage_root),
            "INVESTMENT_RESEARCH_PROCESSING_TIMEOUT_SECONDS": "30",
            "INVESTMENT_RESEARCH_PROCESSING_MEMORY_LIMIT_BYTES": str(512 * 1024 * 1024),
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "investment_research.runner",
            str(capture.processing_run_id),
        ],
        cwd=BACKEND_ROOT,
        env=runner_environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )

    with session_factory() as session:
        run = SqlAlchemyProcessingRepository(session).get_run_by_id(
            context,
            capture.processing_run_id,
        )

    safe_run_diagnostic = (
        "run unavailable"
        if run is None
        else f"state={run.state.value}, safe_summary={run.error_summary!r}"
    )
    assert completed.returncode == 0, f"{completed.stderr}{safe_run_diagnostic}"
    assert completed.stderr == ""
    assert str(capture.processing_run_id) in completed.stdout
    assert ProcessingState.LEXICAL_READY.value in completed.stdout
    assert run is not None
    assert run.state is ProcessingState.LEXICAL_READY
