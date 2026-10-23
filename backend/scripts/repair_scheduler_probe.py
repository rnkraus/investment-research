"""Inspect and exercise the broker-only scheduler inside the isolated queue verifier."""

import json
import subprocess
import time
from collections.abc import Mapping, Sequence

from scripts.compose_health import wait_for_service_health
from scripts.verify_parser_isolation import (
    ParserIsolationVerificationError,
    capture_command,
    inspect_service,
    require_confinement,
    require_mapping,
    run_command,
)

SCHEDULER_TIMEOUT_SECONDS = 90.0
POLL_SECONDS = 0.5
SCHEDULER_EVENTS = (
    "delivery.repair_tick.published",
    "delivery.repair_tick.unavailable",
    "delivery.repair_tick.invalid",
    "delivery.repair_tick.entry_rejected",
)

RUNTIME_PROBE_CODE = """\
import os
import socket
import sys
from pathlib import Path
from redis import Redis
from investment_research.settings import load_scheduler_settings

assert os.getuid() == os.getgid() == 65532
assert {item.name for item in Path("/run/secrets").iterdir()} == {"redis_password"}
assert "investment_research.worker" not in sys.modules
status = Path("/proc/self/status").read_text()
assert "CapEff:\\t0000000000000000" in status and "NoNewPrivs:\\t1" in status
try:
    Path("/app/scheduler-write-probe").touch()
except OSError:
    pass
else:
    raise AssertionError("scheduler source filesystem is writable")
probe = Path("/tmp/scheduler-write-probe")
probe.write_text("temporary")
probe.unlink()
assert not Path("/var/run/docker.sock").exists()
settings = load_scheduler_settings()
assert settings.database_url is None
with Redis.from_url(
    settings.require_redis_url(), socket_connect_timeout=2, socket_timeout=2,
) as broker:
    assert broker.ping()
for host, port in ((sys.argv[1], 5432), ("parser", 8100), ("seaweedfs-s3", 8333), ("1.1.1.1", 443)):
    try:
        socket.create_connection((host, port), timeout=1).close()
    except OSError:
        continue
    raise AssertionError("scheduler reached a forbidden network target")
print("Scheduler runtime permits Redis and denies unrelated credentials, writes, and networks.")
"""


class SchedulerVerificationError(RuntimeError):
    """Safe contract failure without inspected environment or credential details."""


def verify_scheduler_metadata(inspection: Mapping[str, object], *, project_name: str) -> None:
    try:
        config, host = require_confinement(
            inspection, service="scheduler", expected_user="65532:65532"
        )
        if (host.get("Memory"), host.get("NanoCpus"), host.get("PidsLimit")) != (
            256 * 1024 * 1024,
            500_000_000,
            64,
        ):
            raise SchedulerVerificationError("scheduler resource limits differ from the contract")
        if config.get("Entrypoint") != [
            "/app/.venv/bin/python",
            "-m",
            "investment_research.scheduler",
        ]:
            raise SchedulerVerificationError("scheduler does not use its dedicated entry point")
        healthcheck = require_mapping(config, "Healthcheck")
        if healthcheck.get("Test") != [
            "CMD",
            "/app/.venv/bin/python",
            "-m",
            "investment_research.scheduler_health",
        ]:
            raise SchedulerVerificationError("scheduler does not use its local process check")
        environment = config.get("Env")
        if not isinstance(environment, list) or not all(
            isinstance(item, str) for item in environment
        ):
            raise SchedulerVerificationError("scheduler environment metadata is invalid")
        allowed = {
            "INVESTMENT_RESEARCH_ENVIRONMENT",
            "INVESTMENT_RESEARCH_RUNTIME_LOCATION",
            "INVESTMENT_RESEARCH_LOG_LEVEL",
            "REDIS_PASSWORD_FILE",
            "REDIS_PORT",
            "HOME",
            "PATH",
            "PYTHONPATH",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONUNBUFFERED",
            "LANG",
            "GPG_KEY",
            "PYTHON_VERSION",
            "PYTHON_SHA256",
        }
        if any(item.split("=", 1)[0] not in allowed for item in environment):
            raise SchedulerVerificationError("scheduler received unexpected environment values")
        if "REDIS_PASSWORD_FILE=/run/secrets/redis_password" not in environment:
            raise SchedulerVerificationError("scheduler broker secret is not file configured")
        mounts = inspection.get("Mounts")
        if not isinstance(mounts, list):
            raise SchedulerVerificationError("scheduler mount metadata is invalid")
        shared = [
            mount
            for mount in mounts
            if isinstance(mount, dict) and mount.get("Type") in {"bind", "volume"}
        ]
        if (
            len(shared) != 1
            or shared[0].get("Destination") != "/run/secrets/redis_password"
            or shared[0].get("RW") is not False
        ):
            raise SchedulerVerificationError("scheduler has an unexpected shared mount")
        temporary = require_mapping(host, "Tmpfs")
        if set(temporary) != {"/tmp"} or not isinstance(temporary["/tmp"], str):
            raise SchedulerVerificationError("scheduler temporary storage is invalid")
        if set(str(temporary["/tmp"]).split(",")) != {
            "rw",
            "noexec",
            "nosuid",
            "nodev",
            "size=16m",
            "mode=0700",
            "uid=65532",
            "gid=65532",
        }:
            raise SchedulerVerificationError(
                "scheduler temporary storage is not private and bounded"
            )
        networks = require_mapping(require_mapping(inspection, "NetworkSettings"), "Networks")
        if set(networks) != {f"{project_name}_repair"}:
            raise SchedulerVerificationError("scheduler has unexpected network membership")
        if not require_mapping(inspection, "State").get("Running"):
            raise SchedulerVerificationError("scheduler is not running")
    except ParserIsolationVerificationError:
        raise SchedulerVerificationError("scheduler confinement metadata is invalid") from None


def event_counts(
    compose_command: Sequence[str], *, environment: Mapping[str, str]
) -> dict[str, int]:
    """Count only fixed scheduler events from one bounded log read; never print raw content."""

    result = subprocess.run(
        [*compose_command, "logs", "--no-color", "--tail", "200", "scheduler"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    counts = dict.fromkeys(SCHEDULER_EVENTS, 0)
    for line in result.stdout.splitlines():
        _, separator, body = line.partition("|")
        if not separator:
            continue
        try:
            record = json.loads(body)
        except json.JSONDecodeError:
            continue
        name = record.get("event") if isinstance(record, dict) else None
        if isinstance(name, str) and name in counts:
            counts[name] += 1
    return counts


def event_count(
    compose_command: Sequence[str], event: str, *, environment: Mapping[str, str]
) -> int:
    return event_counts(compose_command, environment=environment).get(event, 0)


def wait_for_event(
    compose_command: Sequence[str], event: str, minimum: int, *, environment: Mapping[str, str]
) -> None:
    if event not in SCHEDULER_EVENTS:
        raise SchedulerVerificationError("scheduler event is not allowlisted")
    deadline = time.monotonic() + SCHEDULER_TIMEOUT_SECONDS
    counts = dict.fromkeys(SCHEDULER_EVENTS, 0)
    while time.monotonic() < deadline:
        counts = event_counts(compose_command, environment=environment)
        if counts[event] >= minimum:
            return
        time.sleep(POLL_SECONDS)
    raise SchedulerVerificationError(
        "scheduler did not reach the required periodic event count: "
        f"expected_event={event}, minimum={minimum}, "
        f"published={counts['delivery.repair_tick.published']}, "
        f"unavailable={counts['delivery.repair_tick.unavailable']}, "
        f"invalid={counts['delivery.repair_tick.invalid']}, "
        f"entry_rejected={counts['delivery.repair_tick.entry_rejected']}"
    )


def verify_periodic_scheduler(
    compose_command: Sequence[str], *, project_name: str, environment: Mapping[str, str]
) -> None:
    """Require one isolated scheduler to publish twice and recover from a real broker outage."""

    inspection = inspect_service(compose_command, "scheduler", environment=environment)
    verify_scheduler_metadata(inspection, project_name=project_name)
    if (
        capture_command(
            ["docker", "network", "inspect", f"{project_name}_repair", "--format", "{{.Internal}}"],
            environment=environment,
        )
        != "true"
    ):
        raise SchedulerVerificationError("scheduler network is not internal")
    postgres = inspect_service(compose_command, "postgres", environment=environment)
    backend = require_mapping(
        require_mapping(require_mapping(postgres, "NetworkSettings"), "Networks"),
        f"{project_name}_backend",
    )
    address = backend.get("IPAddress")
    if not isinstance(address, str) or not address:
        raise SchedulerVerificationError("isolated database network address is unavailable")
    run_command(
        "Verify scheduler runtime isolation",
        [
            *compose_command,
            "exec",
            "--no-TTY",
            "scheduler",
            "/app/.venv/bin/python",
            "-c",
            RUNTIME_PROBE_CODE,
            address,
        ],
        environment=environment,
    )
    wait_for_event(compose_command, "delivery.repair_tick.published", 2, environment=environment)
    unavailable = event_count(
        compose_command, "delivery.repair_tick.unavailable", environment=environment
    )
    run_command(
        "Stop isolated Redis during periodic scheduling",
        [*compose_command, "stop", "redis"],
        environment=environment,
    )
    try:
        wait_for_event(
            compose_command,
            "delivery.repair_tick.unavailable",
            unavailable + 1,
            environment=environment,
        )
        published = event_count(
            compose_command, "delivery.repair_tick.published", environment=environment
        )
    finally:
        run_command(
            "Restart isolated Redis after scheduler outage",
            [*compose_command, "start", "redis"],
            environment=environment,
        )
        wait_for_service_health(compose_command, service="redis", environment=environment)
    wait_for_event(
        compose_command, "delivery.repair_tick.published", published + 1, environment=environment
    )
    after = inspect_service(compose_command, "scheduler", environment=environment)
    verify_scheduler_metadata(after, project_name=project_name)
    if after.get("Id") != inspection.get("Id") or after.get("RestartCount") != inspection.get(
        "RestartCount"
    ):
        raise SchedulerVerificationError("scheduler was replaced or restarted during broker outage")
    print(
        "One isolated scheduler published periodically and recovered after Redis returned.",
        flush=True,
    )
