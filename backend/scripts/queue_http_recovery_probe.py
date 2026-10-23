"""Prove automatic recovery of HTTP-captured synthetic sources in one disposable project."""

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx2
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from investment_research.api.contracts import DocumentAccepted, ProcessingRunResult
from investment_research.modules.delivery.domain import OutboxState
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.identity.application import LOCAL_DEVELOPMENT_WORKSPACE_ID
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import (
    ChunkRow,
    ChunkSetRow,
    ProcessingRunRow,
)
from scripts.compose_health import wait_for_service_health
from scripts.repair_scheduler_probe import event_count, wait_for_event
from scripts.verify_parser_isolation import inspect_service, run_command

WAIT_SECONDS = 90.0
POLL_SECONDS = 0.5
TEXT_CONTENT = b"Northstar expanded its European capacity in FY2026."


class HttpRecoveryVerificationError(RuntimeError):
    """Static verification failure, never a raw HTTP, database, or provider response."""


@dataclass(frozen=True)
class CaptureSnapshot:
    state: ProcessingState
    attempts: int
    event_state: OutboxState
    event_attempts: int
    chunk_sets: tuple[UUID, ...]
    chunks: tuple[UUID, ...]


def snapshot(engine: Engine, capture: DocumentAccepted) -> CaptureSnapshot:
    """Read only the exact synthetic run; never repair timestamps, counters, or messages."""

    with Session(engine) as session:
        run = session.scalars(
            select(ProcessingRunRow).where(
                ProcessingRunRow.workspace_id == LOCAL_DEVELOPMENT_WORKSPACE_ID,
                ProcessingRunRow.id == capture.processing_run_id,
                ProcessingRunRow.document_version_id == capture.document_version_id,
            )
        ).one()
        events = session.scalars(
            select(OutboxEventRow).where(
                OutboxEventRow.workspace_id == LOCAL_DEVELOPMENT_WORKSPACE_ID,
                OutboxEventRow.target_id == run.id,
            )
        ).all()
        if len(events) != 1 or events[0].generation != 0:
            raise HttpRecoveryVerificationError("HTTP capture has unexpected delivery history")
        chunk_sets = tuple(
            session.scalars(
                select(ChunkSetRow.id)
                .where(
                    ChunkSetRow.workspace_id == LOCAL_DEVELOPMENT_WORKSPACE_ID,
                    ChunkSetRow.processing_run_id == run.id,
                )
                .order_by(ChunkSetRow.id)
            )
        )
        chunks = tuple(
            session.scalars(
                select(ChunkRow.id)
                .where(
                    ChunkRow.workspace_id == LOCAL_DEVELOPMENT_WORKSPACE_ID,
                    ChunkRow.processing_run_id == run.id,
                )
                .order_by(ChunkRow.id)
            )
        )
        return CaptureSnapshot(
            run.state,
            run.attempt_count,
            events[0].state,
            events[0].attempt_count,
            chunk_sets,
            chunks,
        )


def require_pending(value: CaptureSnapshot) -> None:
    if value != CaptureSnapshot(ProcessingState.CAPTURED, 0, OutboxState.PENDING, 0, (), ()):
        raise HttpRecoveryVerificationError("broker outage changed captured intent or attempts")


def require_recovered(value: CaptureSnapshot, *, expected_chunks: int) -> None:
    if (
        value.state is not ProcessingState.SEARCHABLE
        or value.attempts != 1
        or value.event_state is not OutboxState.PUBLISHED
        or value.event_attempts != 1
        or len(value.chunk_sets) != 1
        or len(value.chunks) != expected_chunks
    ):
        raise HttpRecoveryVerificationError("automatic HTTP recovery lacks one canonical output")


def wait_for_recovery(
    client: httpx2.Client, engine: Engine, captures: tuple[DocumentAccepted, ...]
) -> tuple[CaptureSnapshot, ...]:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        values = tuple(snapshot(engine, capture) for capture in captures)
        for capture in captures:
            response = client.get(capture.processing_status_url)
            response.raise_for_status()
            status = ProcessingRunResult.model_validate(response.json())
            if status.state == "FAILED":
                raise HttpRecoveryVerificationError("HTTP-captured source failed processing")
        if all(
            value.state is ProcessingState.SEARCHABLE and value.event_state is OutboxState.PUBLISHED
            for value in values
        ):
            return values
        time.sleep(POLL_SECONDS)
    raise HttpRecoveryVerificationError("HTTP capture did not recover before timeout")


def verify_http_capture_recovery(
    compose_command: Sequence[str],
    *,
    project_name: str,
    environment: Mapping[str, str],
    engine: Engine,
    api_port: int,
    fixture_pdf: Path,
) -> None:
    """Stop only disposable Redis; wait for the existing scheduler without manual delivery."""

    project_flag = (
        compose_command.index("--project-name") if "--project-name" in compose_command else -1
    )
    if (
        not project_name.startswith("investment-research-queue-check-")
        or project_flag < 0
        or project_flag + 1 >= len(compose_command)
        or compose_command[project_flag + 1] != project_name
        or not 1024 <= api_port <= 65535
    ):
        raise HttpRecoveryVerificationError("HTTP recovery requires an isolated queue project")
    try:
        _verify_http_capture_recovery(
            compose_command,
            project_name=project_name,
            environment=environment,
            engine=engine,
            api_port=api_port,
            fixture_pdf=fixture_pdf,
        )
    except httpx2.HTTPError, ValidationError, SQLAlchemyError, OSError:
        raise HttpRecoveryVerificationError(
            "HTTP recovery transport or persistence check failed"
        ) from None


def _verify_http_capture_recovery(
    compose_command: Sequence[str],
    *,
    project_name: str,
    environment: Mapping[str, str],
    engine: Engine,
    api_port: int,
    fixture_pdf: Path,
) -> None:
    del project_name
    before = {
        service: inspect_service(compose_command, service, environment=environment)
        for service in ("api", "worker", "scheduler")
    }
    unavailable = event_count(
        compose_command, "delivery.repair_tick.unavailable", environment=environment
    )
    with httpx2.Client(
        base_url=f"http://127.0.0.1:{api_port}", timeout=10, trust_env=False
    ) as client:
        run_command(
            "Stop isolated Redis before normal HTTP capture",
            [*compose_command, "stop", "redis"],
            environment=environment,
        )
        try:
            if client.get("/health").status_code != 200 or client.get("/ready").status_code != 503:
                raise HttpRecoveryVerificationError("outage health/readiness contract changed")
            pdf_bytes = fixture_pdf.read_bytes()
            pdf = client.post(
                "/api/v1/documents",
                data={"title": "HTTP outage synthetic PDF", "language": "en"},
                files={"file": (fixture_pdf.name, pdf_bytes, "application/pdf")},
            )
            text = client.post(
                "/api/v1/documents/text",
                json={"title": "HTTP outage synthetic text", "text": TEXT_CONTENT.decode()},
            )
            if pdf.status_code != 202 or text.status_code != 202:
                raise HttpRecoveryVerificationError("HTTP capture was not accepted during outage")
            captures = tuple(
                DocumentAccepted.model_validate(response.json()) for response in (pdf, text)
            )
            for capture, content in zip(captures, (pdf_bytes, TEXT_CONTENT), strict=True):
                if capture.state != "CAPTURED":
                    raise HttpRecoveryVerificationError(
                        "HTTP capture reported premature completion"
                    )
                require_pending(snapshot(engine, capture))
                original = client.get(
                    f"/api/v1/documents/{capture.document_id}"
                    f"/versions/{capture.document_version_id}/original"
                )
                if original.status_code != 200 or original.content != content:
                    raise HttpRecoveryVerificationError(
                        "outage capture lost its immutable original"
                    )
            wait_for_event(
                compose_command,
                "delivery.repair_tick.unavailable",
                unavailable + 1,
                environment=environment,
            )
            for capture in captures:
                require_pending(snapshot(engine, capture))
        finally:
            run_command(
                "Restart isolated Redis for automatic HTTP recovery",
                [*compose_command, "start", "redis"],
                environment=environment,
            )
            wait_for_service_health(compose_command, service="redis", environment=environment)
        values = wait_for_recovery(client, engine, captures)
        for value, expected in zip(values, (4, 1), strict=True):
            require_recovered(value, expected_chunks=expected)
        published = event_count(
            compose_command, "delivery.repair_tick.published", environment=environment
        )
        wait_for_event(
            compose_command,
            "delivery.repair_tick.published",
            published + 1,
            environment=environment,
        )
        if tuple(snapshot(engine, capture) for capture in captures) != values:
            raise HttpRecoveryVerificationError("a later periodic tick duplicated canonical output")
    for service, previous in before.items():
        current = inspect_service(compose_command, service, environment=environment)
        if current.get("Id") != previous.get("Id") or current.get("RestartCount") != previous.get(
            "RestartCount"
        ):
            raise HttpRecoveryVerificationError("HTTP recovery restarted an application process")
    print(
        "HTTP PDF/text capture survived Redis outage and recovered automatically "
        "with one canonical output per source.",
        flush=True,
    )
