"""Bounded health waiting for an existing service, independent of Compose start flags."""

import subprocess
import time
from collections.abc import Mapping, Sequence

HEALTH_TIMEOUT_SECONDS = 60.0
POLL_SECONDS = 0.25
STATE_FORMAT = "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}"


class ComposeHealthVerificationError(RuntimeError):
    """A static readiness failure that never includes inspected content or credentials."""


def wait_for_service_health(
    compose_command: Sequence[str], *, service: str, environment: Mapping[str, str]
) -> None:
    """Require the same existing container to become healthy within a single deadline."""

    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS

    def capture(command: Sequence[str]) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ComposeHealthVerificationError("service health deadline exceeded")
        try:
            result = subprocess.run(
                command,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=min(5.0, remaining),
            )
        except OSError, subprocess.SubprocessError:
            raise ComposeHealthVerificationError("service health inspection failed") from None
        return result.stdout.strip()

    identifier = capture([*compose_command, "ps", "--all", "--quiet", service])
    if not identifier or not identifier.isascii() or not identifier.isalnum():
        raise ComposeHealthVerificationError("expected exactly one existing service container")
    while time.monotonic() < deadline:
        state = capture(["docker", "inspect", "--format", STATE_FORMAT, identifier])
        if time.monotonic() >= deadline:
            break
        if state == "running healthy":
            return
        if state != "running starting":
            raise ComposeHealthVerificationError("service is not running with a valid healthcheck")
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    raise ComposeHealthVerificationError("service health deadline exceeded")
