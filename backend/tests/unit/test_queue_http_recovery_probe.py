"""HTTP outage verification must preserve isolation, failure evidence, and cleanup."""

import time
from dataclasses import replace
from pathlib import Path
from typing import TypedDict, cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx2
import pytest

from investment_research.api.contracts import DocumentAccepted
from investment_research.modules.delivery.domain import OutboxState
from investment_research.modules.processing.domain import ProcessingState
from scripts import queue_http_recovery_probe as probe

PROJECT = "investment-research-queue-check-12345678"
COMPOSE = ["docker", "compose", "--project-name", PROJECT]
PENDING = probe.CaptureSnapshot(ProcessingState.CAPTURED, 0, OutboxState.PENDING, 0, (), ())


class SnapshotChanges(TypedDict, total=False):
    state: ProcessingState
    attempts: int
    event_state: OutboxState
    event_attempts: int
    chunk_sets: tuple[UUID, ...]
    chunks: tuple[UUID, ...]


def capture() -> DocumentAccepted:
    run_id = uuid4()
    return DocumentAccepted(
        document_id=uuid4(),
        document_version_id=uuid4(),
        processing_run_id=run_id,
        state="CAPTURED",
        processing_status_url=f"/api/v1/processing-runs/{run_id}",
    )


def recovered(chunks: int) -> probe.CaptureSnapshot:
    return probe.CaptureSnapshot(
        ProcessingState.SEARCHABLE,
        1,
        OutboxState.PUBLISHED,
        1,
        (uuid4(),),
        tuple(uuid4() for _ in range(chunks)),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"state": ProcessingState.PARSING},
        {"attempts": 1},
        {"event_state": OutboxState.CLAIMED},
        {"event_attempts": 1},
        {"chunk_sets": (uuid4(),)},
        {"chunks": (uuid4(),)},
    ],
)
def test_outage_snapshot_rejects_consumed_attempts_or_premature_output(
    changes: SnapshotChanges,
) -> None:
    probe.require_pending(PENDING)
    with pytest.raises(probe.HttpRecoveryVerificationError):
        probe.require_pending(replace(PENDING, **changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"state": ProcessingState.CAPTURED},
        {"attempts": 2},
        {"event_state": OutboxState.PENDING},
        {"event_attempts": 2},
        {"chunk_sets": ()},
        {"chunk_sets": (uuid4(), uuid4())},
        {"chunks": ()},
    ],
)
def test_recovery_requires_exactly_one_canonical_attempt_and_output(
    changes: SnapshotChanges,
) -> None:
    value = recovered(4)
    probe.require_recovered(value, expected_chunks=4)
    with pytest.raises(probe.HttpRecoveryVerificationError):
        probe.require_recovered(replace(value, **changes), expected_chunks=4)


@pytest.mark.parametrize(
    ("command", "project", "port"),
    [
        ([], PROJECT, 58001),
        (["docker", "compose", "--project-name"], PROJECT, 58001),
        (COMPOSE, "investment-research-m0-0", 58001),
        (["docker", "compose", "--project-name", "other"], PROJECT, 58001),
        (COMPOSE, PROJECT, 80),
        (COMPOSE, PROJECT, 65536),
    ],
)
def test_nonisolated_invocations_do_not_touch_http_docker_or_database(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    project: str,
    port: int,
) -> None:
    verify = Mock()
    monkeypatch.setattr(probe, "_verify_http_capture_recovery", verify)
    with pytest.raises(probe.HttpRecoveryVerificationError):
        probe.verify_http_capture_recovery(
            command,
            project_name=project,
            environment={},
            engine=Mock(),
            api_port=port,
            fixture_pdf=Path("not-opened.pdf"),
        )
    verify.assert_not_called()


def install_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Mock, Mock, Mock, Path]:
    pdf, text = capture(), capture()
    fixture = tmp_path / "synthetic.pdf"
    fixture.write_bytes(b"%PDF-synthetic")
    client = Mock()
    pdf_original = (
        f"/api/v1/documents/{pdf.document_id}/versions/{pdf.document_version_id}/original"
    )
    text_original = (
        f"/api/v1/documents/{text.document_id}/versions/{text.document_version_id}/original"
    )
    responses = {
        "/health": httpx2.Response(200),
        "/ready": httpx2.Response(503),
        pdf_original: httpx2.Response(200, content=fixture.read_bytes()),
        text_original: httpx2.Response(200, content=probe.TEXT_CONTENT),
    }
    client.get.side_effect = responses.__getitem__
    client.post.side_effect = [
        httpx2.Response(202, json=pdf.model_dump(mode="json")),
        httpx2.Response(202, json=text.model_dump(mode="json")),
    ]
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(httpx2, "Client", factory)
    metadata = {"Id": "unchanged", "RestartCount": 0}
    monkeypatch.setattr(probe, "inspect_service", Mock(return_value=metadata))
    monkeypatch.setattr(probe, "event_count", Mock(side_effect=[0, 4]))
    wait = Mock()
    monkeypatch.setattr(probe, "wait_for_event", wait)
    command = Mock()
    monkeypatch.setattr(probe, "run_command", command)
    values = (recovered(4), recovered(1))
    monkeypatch.setattr(probe, "snapshot", Mock(side_effect=[PENDING] * 4 + list(values)))
    monkeypatch.setattr(probe, "wait_for_recovery", Mock(return_value=values))
    return client, command, wait, fixture


def test_http_capture_uses_existing_schedule_without_manual_delivery_and_rechecks_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, command, wait, fixture = install_fakes(monkeypatch, tmp_path)
    probe.verify_http_capture_recovery(
        COMPOSE,
        project_name=PROJECT,
        environment={},
        engine=Mock(),
        api_port=58001,
        fixture_pdf=fixture,
    )
    assert [call.args[1] for call in command.call_args_list] == [
        [*COMPOSE, "stop", "redis"],
        [*COMPOSE, "start", "--wait", "redis"],
    ]
    assert [call.args[0] for call in client.post.call_args_list] == [
        "/api/v1/documents",
        "/api/v1/documents/text",
    ]
    assert [call.args[1:] for call in wait.call_args_list] == [
        ("delivery.repair_tick.unavailable", 1),
        ("delivery.repair_tick.published", 5),
    ]
    cast(Mock, httpx2.Client).assert_called_once_with(
        base_url="http://127.0.0.1:58001",
        timeout=10,
        trust_env=False,
    )


@pytest.mark.parametrize("failure", ["http", "scheduler", "changed_output", "restarted"])
def test_failures_are_not_success_and_redis_restart_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    client, command, wait, fixture = install_fakes(monkeypatch, tmp_path)
    error_type: type[Exception] = probe.HttpRecoveryVerificationError
    if failure == "http":
        client.post.side_effect = httpx2.ConnectError("must-not-appear")
    elif failure == "scheduler":
        from scripts.repair_scheduler_probe import SchedulerVerificationError

        error_type = SchedulerVerificationError
        wait.side_effect = error_type("synthetic unavailable tick missing")
    elif failure == "changed_output":
        monkeypatch.setattr(probe, "snapshot", Mock(side_effect=[PENDING] * 6))
    else:
        monkeypatch.setattr(
            probe,
            "inspect_service",
            Mock(
                side_effect=[
                    {"Id": "same", "RestartCount": 0},
                    {"Id": "same", "RestartCount": 0},
                    {"Id": "same", "RestartCount": 0},
                    {"Id": "different", "RestartCount": 0},
                ]
            ),
        )
    with pytest.raises(error_type) as error:
        probe.verify_http_capture_recovery(
            COMPOSE,
            project_name=PROJECT,
            environment={},
            engine=Mock(),
            api_port=58001,
            fixture_pdf=fixture,
        )
    assert "must-not-appear" not in str(error.value)
    assert command.call_args.args[1] == [*COMPOSE, "start", "--wait", "redis"]


@pytest.mark.parametrize("failed", [False, True])
def test_recovery_wait_stops_on_failure_or_deadline(
    monkeypatch: pytest.MonkeyPatch,
    failed: bool,
) -> None:
    source = capture()
    monkeypatch.setattr(probe, "snapshot", Mock(return_value=PENDING))
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, 0, probe.WAIT_SECONDS]))
    monkeypatch.setattr(time, "sleep", Mock())
    client = Mock()
    client.get.return_value.json.return_value = {
        "processing_run_id": str(source.processing_run_id),
        "document_version_id": str(source.document_version_id),
        "state": "FAILED" if failed else "CAPTURED",
        "error_summary": None,
    }
    with pytest.raises(probe.HttpRecoveryVerificationError):
        probe.wait_for_recovery(client, Mock(), (source,))
