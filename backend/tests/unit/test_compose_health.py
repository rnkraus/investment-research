"""Readiness waiting must work without Compose start --wait and remain bounded and fail closed."""

import subprocess
import time
from unittest.mock import Mock

import pytest

from scripts import compose_health as health

COMPOSE = ["docker", "compose", "--project-name", "investment-research-queue-check-test"]


def fake_run(monkeypatch: pytest.MonkeyPatch, outputs: list[str | Exception]) -> Mock:
    run = Mock(
        side_effect=[
            value if isinstance(value, Exception) else subprocess.CompletedProcess([], 0, value)
            for value in outputs
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(time, "sleep", Mock())
    return run


def test_existing_container_is_polled_until_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    run = fake_run(monkeypatch, ["abc123", "running starting", "running healthy"])
    health.wait_for_service_health(COMPOSE, service="redis", environment={})
    assert run.call_args_list[0].args[0] == [*COMPOSE, "ps", "--all", "--quiet", "redis"]
    for call in run.call_args_list[1:]:
        assert call.args[0] == ["docker", "inspect", "--format", health.STATE_FORMAT, "abc123"]
        assert 0 < call.kwargs["timeout"] <= 5


@pytest.mark.parametrize("identifier", ["", "abc\ndef", "../abc", "--bad", "å"])
def test_missing_multiple_or_invalid_containers_are_rejected(
    monkeypatch: pytest.MonkeyPatch, identifier: str
) -> None:
    run = fake_run(monkeypatch, [identifier])
    with pytest.raises(health.ComposeHealthVerificationError):
        health.wait_for_service_health(COMPOSE, service="redis", environment={})
    assert run.call_count == 1


@pytest.mark.parametrize(
    "state", ["running", "running unhealthy", "exited healthy", "restarting", "must-not-appear"]
)
def test_unhealthy_exited_or_missing_healthchecks_never_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    fake_run(monkeypatch, ["abc123", state])
    with pytest.raises(health.ComposeHealthVerificationError) as error:
        health.wait_for_service_health(COMPOSE, service="redis", environment={})
    assert "must-not-appear" not in str(error.value)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "failure",
    [
        OSError("must-not-appear"),
        subprocess.CalledProcessError(1, "must-not-appear"),
        subprocess.TimeoutExpired("must-not-appear", 5),
    ],
)
def test_inspection_failures_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    fake_run(monkeypatch, ["abc123", failure])
    with pytest.raises(health.ComposeHealthVerificationError) as error:
        health.wait_for_service_health(COMPOSE, service="redis", environment={})
    assert "must-not-appear" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("state", ["running starting", "running healthy"])
def test_deadline_is_checked_even_before_success(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    run = fake_run(monkeypatch, ["abc123", state])
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, 0, 59, 59, 61]))
    with pytest.raises(health.ComposeHealthVerificationError, match="deadline"):
        health.wait_for_service_health(COMPOSE, service="redis", environment={})
    assert run.call_args.kwargs["timeout"] == 1
