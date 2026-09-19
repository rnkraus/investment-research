"""Verify the complete M0 fixture journey through the isolated runtime."""

import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

from investment_research.infrastructure.generation.deterministic import M0_0_FIXTURE_ANSWER_CASE
from scripts.docker_secrets import (
    COMPOSE_SECRET_FILES,
    disposable_compose_secret_environment,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
COMPOSE_TEST_FILE = REPOSITORY_ROOT / "compose.test.yaml"
FIXTURE_PDF = (
    REPOSITORY_ROOT / "fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf"
)
ACCEPTANCE_TEST = "tests/acceptance/test_m0_runtime_journey.py"
POSTGRES_DATABASE = "investment_research"
POSTGRES_USER = "investment_research"
SECRET_LOG_MARKER = "m0-acceptance-secret-marker"
REDACTED_LOG_VALUE = b"[REDACTED]"
DOCKER_IMAGE_NOT_FOUND_MARKER = b"No such image:"


class SensitiveRuntimeLogError(RuntimeError):
    """Report sensitive runtime logging without reproducing the logged value."""


def find_docker_executable() -> str | None:
    """Return the Docker executable path when it is available."""

    return shutil.which("docker")


def generate_disposable_credential() -> str:
    """Return one random credential used only by an isolated runtime."""

    return f"{SECRET_LOG_MARKER}-{secrets.token_hex(24)}"


def find_available_loopback_port() -> int:
    """Reserve and release one currently available local TCP port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def run_command(
    label: str,
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    working_directory: Path = REPOSITORY_ROOT,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Run one visible command without rendering its environment values."""

    print(f"\n==> {label}", flush=True)
    print(f"$ {shlex.join(command)}", flush=True)
    started_at = time.monotonic()
    result = subprocess.run(
        command,
        cwd=working_directory,
        env=environment,
        check=check,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.STDOUT if capture_output else None,
    )
    print(f"<== {label} completed in {time.monotonic() - started_at:.2f}s", flush=True)
    return result


def capture_runtime_logs(
    compose_command: Sequence[str],
    environment: Mapping[str, str],
) -> bytes:
    """Capture application logs without writing their untrusted content to the host output."""

    result = run_command(
        "Capture isolated runtime logs for sensitive-content verification",
        [*compose_command, "logs", "--no-color", "web", "api", "worker", "parser"],
        environment=environment,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args)
    return result.stdout or b""


def sensitive_log_values(secret_values: Mapping[str, str]) -> tuple[bytes, ...]:
    """Return exact synthetic values that must never appear in runtime logs."""

    return (
        SECRET_LOG_MARKER.encode(),
        *(value.encode() for value in secret_values.values()),
        M0_0_FIXTURE_ANSWER_CASE.evidence_text.encode(),
    )


def remove_acceptance_image(
    image: str,
    environment: Mapping[str, str],
) -> int:
    """Remove one disposable image while accepting an already-absent result."""

    result = run_command(
        "Remove isolated M0 acceptance image",
        ["docker", "image", "rm", "--force", image],
        environment=environment,
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        return 0
    if DOCKER_IMAGE_NOT_FOUND_MARKER in (result.stdout or b""):
        print("Isolated M0 acceptance image was already absent.", flush=True)
        return 0
    print("Could not remove isolated M0 acceptance image.", file=sys.stderr, flush=True)
    return result.returncode or 1


def assert_runtime_logs_are_safe(log_output: bytes, secret_values: Mapping[str, str]) -> None:
    """Fail without disclosure when logs contain a credential marker or original text."""

    for value in sensitive_log_values(secret_values):
        if value and value in log_output:
            raise SensitiveRuntimeLogError("isolated runtime logs contain sensitive content")


def redact_runtime_logs(log_output: bytes, secret_values: Mapping[str, str]) -> str:
    """Remove every known sensitive sentinel before failure diagnostics are displayed."""

    redacted = log_output
    for value in sorted(sensitive_log_values(secret_values), key=len, reverse=True):
        if value:
            redacted = redacted.replace(value, REDACTED_LOG_VALUE)
    return redacted.decode(errors="replace")


def verify_m0_runtime() -> int:
    """Create, verify, and always remove one complete isolated M0 runtime."""

    if find_docker_executable() is None:
        print("Docker is required for M0 runtime verification.", file=sys.stderr)
        return 127
    if not FIXTURE_PDF.is_file():
        print("The frozen M0.0 PDF fixture is unavailable.", file=sys.stderr)
        return 2
    if not COMPOSE_TEST_FILE.is_file():
        print("The deterministic Compose test override is unavailable.", file=sys.stderr)
        return 2

    run_id = uuid4().hex[:8]
    project_name = f"investment-research-m0-check-{run_id}"
    acceptance_image = f"investment-research-m0-acceptance:{run_id}"
    postgres_port = find_available_loopback_port()
    api_port = find_available_loopback_port()
    web_port = find_available_loopback_port()
    secret_values = {name: generate_disposable_credential() for name in COMPOSE_SECRET_FILES}
    compose_command = [
        "docker",
        "compose",
        "--project-name",
        project_name,
        "--file",
        str(COMPOSE_FILE),
        "--file",
        str(COMPOSE_TEST_FILE),
    ]
    exit_code = 0
    runtime_logs = b""

    with disposable_compose_secret_environment(secret_values) as secret_sources:
        compose_environment = os.environ.copy() | secret_sources
        compose_environment.update(
            {
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "POSTGRES_DB": POSTGRES_DATABASE,
                "POSTGRES_USER": POSTGRES_USER,
                "POSTGRES_PORT": str(postgres_port),
                "INVESTMENT_RESEARCH_API_PORT": str(api_port),
                "INVESTMENT_RESEARCH_WEB_PORT": str(web_port),
                "INVESTMENT_RESEARCH_S3_BUCKET": f"m0-acceptance-{run_id}",
                "INVESTMENT_RESEARCH_S3_REGION_NAME": "us-east-1",
                "M0_ACCEPTANCE_IMAGE": acceptance_image,
                "M0_ACCEPTANCE_FIXTURE_PATH": str(FIXTURE_PDF),
            }
        )
        try:
            run_command(
                "Build and start isolated complete M0 runtime",
                [
                    *compose_command,
                    "up",
                    "--build",
                    "--detach",
                    "--wait",
                    "web",
                    "worker",
                ],
                environment=compose_environment,
            )
            run_command(
                "Seed isolated PostgreSQL through the internal tool container",
                [
                    *compose_command,
                    "--profile",
                    "tools",
                    "run",
                    "--build",
                    "--rm",
                    "--no-deps",
                    "seed",
                ],
                environment=compose_environment,
            )
            run_command(
                "Build isolated M0 acceptance image",
                [*compose_command, "build", "acceptance"],
                environment=compose_environment,
            )
            run_command(
                "Run automatic M0 fixture journey",
                [
                    *compose_command,
                    "run",
                    "--rm",
                    "--no-deps",
                    "acceptance",
                    "-q",
                    "-s",
                    "-p",
                    "no:cacheprovider",
                    ACCEPTANCE_TEST,
                ],
                environment=compose_environment,
            )
            runtime_logs = capture_runtime_logs(compose_command, compose_environment)
            assert_runtime_logs_are_safe(runtime_logs, secret_values)
            print("Automatic M0 fixture journey passed.", flush=True)
        except (SensitiveRuntimeLogError, subprocess.CalledProcessError) as error:
            if isinstance(error, subprocess.CalledProcessError):
                exit_code = error.returncode or 1
            else:
                exit_code = 1
            if not runtime_logs:
                try:
                    runtime_logs = capture_runtime_logs(compose_command, compose_environment)
                except subprocess.CalledProcessError:
                    runtime_logs = b"Runtime logs were unavailable."
            print(redact_runtime_logs(runtime_logs, secret_values), file=sys.stderr, flush=True)
        finally:
            cleanup = run_command(
                "Remove isolated M0 runtime and its volumes",
                [*compose_command, "down", "--volumes", "--remove-orphans"],
                environment=compose_environment,
                check=False,
            )
            if cleanup.returncode != 0 and exit_code == 0:
                exit_code = cleanup.returncode
            image_cleanup_code = remove_acceptance_image(
                acceptance_image,
                compose_environment,
            )
            if image_cleanup_code != 0 and exit_code == 0:
                exit_code = image_cleanup_code

    return exit_code


def main() -> int:
    """Run the complete M0 runtime verification command."""

    return verify_m0_runtime()


if __name__ == "__main__":
    raise SystemExit(main())
