"""Scheduler Compose isolation and periodic/outage evidence must fail closed."""

import subprocess
import time
from unittest.mock import Mock

import pytest

from scripts import repair_scheduler_probe as probe
from scripts.compose_health import ComposeHealthVerificationError

PROJECT = "isolated-scheduler-test"


def inspection() -> dict[str, object]:
    return {
        "Id": "same-scheduler",
        "RestartCount": 0,
        "State": {"Running": True},
        "Config": {
            "User": "65532:65532",
            "Entrypoint": ["/app/.venv/bin/python", "-m", "investment_research.scheduler"],
            "Healthcheck": {
                "Test": [
                    "CMD",
                    "/app/.venv/bin/python",
                    "-m",
                    "investment_research.scheduler_health",
                ]
            },
            "Env": ["REDIS_PASSWORD_FILE=/run/secrets/redis_password", "REDIS_PORT=6379"],
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PortBindings": {},
            "Memory": 256 * 1024 * 1024,
            "NanoCpus": 500_000_000,
            "PidsLimit": 64,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=65532,gid=65532"},
        },
        "Mounts": [{"Type": "bind", "Destination": "/run/secrets/redis_password", "RW": False}],
        "NetworkSettings": {"Networks": {f"{PROJECT}_repair": {}}},
    }


def test_metadata_accepts_exact_broker_only_confinement() -> None:
    probe.verify_scheduler_metadata(inspection(), project_name=PROJECT)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("Config", "User", "0:0"),
        ("Config", "Entrypoint", ["celery", "worker"]),
        ("Config", "Healthcheck", {"Test": ["NONE"]}),
        ("Config", "Healthcheck", {"Test": ["CMD", "true"]}),
        ("Config", "Env", ["POSTGRES_PASSWORD_FILE=/must-not-be-opened"]),
        ("Config", "Env", ["INVESTMENT_RESEARCH_OPENAI_QUERY_API_KEY=must-not-appear"]),
        ("Config", "Env", ["REDIS_PASSWORD=must-not-appear"]),
        ("HostConfig", "ReadonlyRootfs", False),
        ("HostConfig", "CapDrop", []),
        ("HostConfig", "SecurityOpt", []),
        ("HostConfig", "Memory", 0),
        ("HostConfig", "NanoCpus", 0),
        ("HostConfig", "PidsLimit", 0),
        ("HostConfig", "PortBindings", {"8000/tcp": [{"HostPort": "8000"}]}),
        ("HostConfig", "Tmpfs", {"/tmp": "rw,size=1g"}),
        ("NetworkSettings", "Networks", {f"{PROJECT}_backend": {}}),
        ("State", "Running", False),
    ],
)
def test_metadata_rejects_expanded_authority_without_echoing_values(
    section: str, field: str, value: object
) -> None:
    metadata = inspection()
    target = metadata[section]
    assert isinstance(target, dict)
    target[field] = value
    with pytest.raises(probe.SchedulerVerificationError) as error:
        probe.verify_scheduler_metadata(metadata, project_name=PROJECT)
    assert "must-not-appear" not in str(error.value)


@pytest.mark.parametrize(
    "mounts",
    [
        [],
        [{"Type": "bind", "Destination": "/workspace", "RW": False}],
        [{"Type": "bind", "Destination": "/run/secrets/redis_password", "RW": True}],
        [{"Type": "volume", "Destination": "/data", "RW": True}],
    ],
)
def test_metadata_rejects_missing_or_unexpected_mounts(mounts: list[dict[str, object]]) -> None:
    metadata = inspection()
    metadata["Mounts"] = mounts
    with pytest.raises(probe.SchedulerVerificationError):
        probe.verify_scheduler_metadata(metadata, project_name=PROJECT)


def test_runtime_probe_exercises_filesystem_secret_and_network_denials() -> None:
    code = probe.RUNTIME_PROBE_CODE
    compile(code, "scheduler-runtime-probe", "exec")
    assert '== {"redis_password"}' in code
    assert "broker.ping()" in code
    assert "(sys.argv[1], 5432)" in code
    assert '("1.1.1.1", 443)' in code
    assert "socket_connect_timeout=2" in code
    assert "os.getuid() == os.getgid() == 65532" in code


def install_periodic_fakes(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock, Mock, Mock]:
    metadata = inspection()
    postgres = {
        "NetworkSettings": {"Networks": {f"{PROJECT}_backend": {"IPAddress": "172.20.0.2"}}}
    }
    inspect = Mock(side_effect=[metadata, postgres, metadata])
    command = Mock()
    wait = Mock()
    # The post-outage baseline may exceed earlier publications; require a fresh success after it.
    count = Mock(side_effect=[0, 4])
    monkeypatch.setattr(probe, "inspect_service", inspect)
    monkeypatch.setattr(probe, "capture_command", Mock(return_value="true"))
    monkeypatch.setattr(probe, "run_command", command)
    monkeypatch.setattr(probe, "wait_for_event", wait)
    monkeypatch.setattr(probe, "event_count", count)
    monkeypatch.setattr(probe, "wait_for_service_health", Mock())
    return inspect, command, wait, count


def test_periodic_probe_requires_repeat_and_new_success_after_actual_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, command, wait, _ = install_periodic_fakes(monkeypatch)
    health = Mock()
    monkeypatch.setattr(probe, "wait_for_service_health", health)
    probe.verify_periodic_scheduler(["docker", "compose"], project_name=PROJECT, environment={})
    assert [call.args[1:] for call in wait.call_args_list] == [
        ("delivery.repair_tick.published", 2),
        ("delivery.repair_tick.unavailable", 1),
        ("delivery.repair_tick.published", 5),
    ]
    assert [call.args[0] for call in command.call_args_list] == [
        "Verify scheduler runtime isolation",
        "Stop isolated Redis during periodic scheduling",
        "Restart isolated Redis after scheduler outage",
    ]
    assert command.call_args.args[1] == ["docker", "compose", "start", "redis"]
    health.assert_called_once_with(["docker", "compose"], service="redis", environment={})


def test_outage_probe_restarts_redis_even_if_failure_evidence_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, command, wait, _ = install_periodic_fakes(monkeypatch)
    wait.side_effect = [None, probe.SchedulerVerificationError("synthetic missing failure event")]
    with pytest.raises(probe.SchedulerVerificationError):
        probe.verify_periodic_scheduler(["docker", "compose"], project_name=PROJECT, environment={})
    assert command.call_args.args[0] == "Restart isolated Redis after scheduler outage"


def test_redis_health_failure_cannot_confirm_scheduler_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, wait, _ = install_periodic_fakes(monkeypatch)
    monkeypatch.setattr(
        probe,
        "wait_for_service_health",
        Mock(side_effect=ComposeHealthVerificationError("synthetic Redis health failure")),
    )
    with pytest.raises(ComposeHealthVerificationError):
        probe.verify_periodic_scheduler(["docker", "compose"], project_name=PROJECT, environment={})
    assert wait.call_count == 2  # No successful post-outage event can bypass failed readiness.


@pytest.mark.parametrize("change", [{"Id": "replacement"}, {"RestartCount": 1}])
def test_outage_recovery_cannot_be_explained_by_a_replacement_or_restart(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    inspect, _, _, _ = install_periodic_fakes(monkeypatch)
    original = inspection()
    postgres = {
        "NetworkSettings": {"Networks": {f"{PROJECT}_backend": {"IPAddress": "172.20.0.2"}}}
    }
    inspect.side_effect = [original, postgres, original | change]
    with pytest.raises(probe.SchedulerVerificationError, match="replaced or restarted"):
        probe.verify_periodic_scheduler(["docker", "compose"], project_name=PROJECT, environment={})


def test_event_counter_accepts_only_exact_structured_events_without_printing_logs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = "\n".join(
        [
            'scheduler-1 | {"event":"delivery.repair_tick.published"}',
            'scheduler-1 | {"event":"delivery.repair_tick.published.extra"}',
            'scheduler-1 | {"event":[]}',
            'scheduler-1 | {"event":"must-not-appear"}',
            "scheduler-1 | unstructured must-not-appear",
            "scheduler-1 | []",
        ]
    )
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=output))
    monkeypatch.setattr(subprocess, "run", run)
    assert (
        probe.event_count(["docker", "compose"], "delivery.repair_tick.published", environment={})
        == 1
    )
    assert run.call_args.kwargs["timeout"] == 5
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("event", probe.SCHEDULER_EVENTS)
def test_event_wait_is_bounded_and_reports_only_fixed_events_and_counters(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], event: str
) -> None:
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, 0, 91]))
    monkeypatch.setattr(time, "sleep", Mock())
    output = "\n".join(
        [
            'scheduler-1 | {"event":"delivery.repair_tick.published","detail":"must-not-appear"}',
            'scheduler-1 | {"event":"delivery.repair_tick.unavailable"}',
            'scheduler-1 | {"event":"delivery.repair_tick.unavailable"}',
            'scheduler-1 | {"event":"delivery.repair_tick.invalid"}',
            'scheduler-1 | {"event":"delivery.repair_tick.entry_rejected"}',
            'scheduler-1 | {"event":"must-not-appear"}',
            "scheduler-1 | unstructured must-not-appear",
        ]
    )
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=output))
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(probe.SchedulerVerificationError) as error:
        probe.wait_for_event([], event, 3, environment={})
    assert str(error.value) == (
        "scheduler did not reach the required periodic event count: "
        f"expected_event={event}, minimum=3, published=1, unavailable=2, "
        "invalid=1, entry_rejected=1"
    )
    assert run.call_count == 1  # No extra log reads or waiting after the existing deadline.
    assert run.call_args.kwargs["timeout"] == 5
    assert capsys.readouterr() == ("", "")


def test_successful_event_wait_remains_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, 0]))
    sleep = Mock()
    monkeypatch.setattr(time, "sleep", sleep)
    output = 'scheduler-1 | {"event":"delivery.repair_tick.published"}\n' * 2
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=output))
    monkeypatch.setattr(subprocess, "run", run)
    probe.wait_for_event([], "delivery.repair_tick.published", 2, environment={})
    assert run.call_count == 1
    sleep.assert_not_called()
    assert capsys.readouterr() == ("", "")


def test_unapproved_event_cannot_enter_timeout_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(probe.SchedulerVerificationError) as error:
        probe.wait_for_event([], "must-not-appear", 2, environment={})
    assert "must-not-appear" not in str(error.value)
    run.assert_not_called()
