"""Verify the complete credential-free M1 journey through an isolated runtime."""

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

from investment_research.infrastructure.acceptance import M1_TEXT_FIXTURE_ANSWER_CASE
from investment_research.infrastructure.generation.deterministic import M0_0_FIXTURE_ANSWER_CASE
from scripts.docker_secrets import (
    COMPOSE_SECRET_FILES,
    disposable_compose_secret_environment,
)
from scripts.verify_m0_runtime import (
    REDACTED_LOG_VALUE,
    SensitiveRuntimeLogError,
    find_available_loopback_port,
    find_docker_executable,
    generate_disposable_credential,
    run_command,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
COMPOSE_TEST_FILE = REPOSITORY_ROOT / "compose.test.yaml"
COMPOSE_M1_FILE = REPOSITORY_ROOT / "compose.m1-acceptance.yaml"
FIXTURE_PDF = (
    REPOSITORY_ROOT / "fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf"
)
FIXTURE_TEXT = (
    REPOSITORY_ROOT / "fixtures/m1/documents/northstar-grid-analytics-fy2026-capacity-update.txt"
)
ACCEPTANCE_TEST = "tests/acceptance/test_m1_runtime_journey.py"
POSTGRES_DATABASE = "investment_research"
POSTGRES_USER = "investment_research"
SECRET_LOG_MARKER = "m1-acceptance-secret-marker"
DOCKER_IMAGE_NOT_FOUND_MARKER = b"No such image:"


def find_pnpm_executable() -> str | None:
    """Locate the project-required frontend package manager."""

    return shutil.which("pnpm")


def sensitive_log_values(secret_values: Mapping[str, str]) -> tuple[bytes, ...]:
    """Return exact synthetic credentials and evidence that logs must not disclose."""

    return (
        SECRET_LOG_MARKER.encode(),
        *(value.encode() for value in secret_values.values()),
        M0_0_FIXTURE_ANSWER_CASE.evidence_text.encode(),
        M1_TEXT_FIXTURE_ANSWER_CASE.evidence_text.encode(),
    )


def capture_runtime_logs(
    compose_command: Sequence[str],
    environment: Mapping[str, str],
) -> bytes:
    """Capture application logs without rendering untrusted content to host output."""

    result = run_command(
        "Capture isolated M1 runtime logs for sensitive-content verification",
        [*compose_command, "logs", "--no-color", "web", "api", "worker", "parser"],
        environment=environment,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args)
    return result.stdout or b""


def assert_runtime_logs_are_safe(
    log_output: bytes,
    secret_values: Mapping[str, str],
) -> None:
    """Fail without disclosure when runtime logs contain a protected value."""

    for value in sensitive_log_values(secret_values):
        if value and value in log_output:
            raise SensitiveRuntimeLogError("isolated M1 runtime logs contain sensitive content")


def redact_runtime_logs(
    log_output: bytes,
    secret_values: Mapping[str, str],
) -> str:
    """Remove every known sensitive sentinel before failure diagnostics are displayed."""

    redacted = log_output
    for value in sorted(sensitive_log_values(secret_values), key=len, reverse=True):
        if value:
            redacted = redacted.replace(value, REDACTED_LOG_VALUE)
    return redacted.decode(errors="replace")


def remove_acceptance_image(
    image: str,
    environment: Mapping[str, str],
) -> int:
    """Remove one uniquely named image while accepting an already-absent result."""

    result = run_command(
        "Remove isolated M1 acceptance image",
        ["docker", "image", "rm", image],
        environment=environment,
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        return 0
    if DOCKER_IMAGE_NOT_FOUND_MARKER in (result.stdout or b""):
        print("Isolated M1 acceptance image was already absent.", flush=True)
        return 0
    print("Could not remove isolated M1 acceptance image.", file=sys.stderr, flush=True)
    return result.returncode or 1


def verify_m1_runtime(*, skip_browser: bool = False) -> int:
    """Create, verify, and always remove one complete isolated M1 runtime."""

    if find_docker_executable() is None:
        print("Docker is required for M1 runtime verification.", file=sys.stderr)
        return 127
    pnpm_executable = None if skip_browser else find_pnpm_executable()
    if not skip_browser and pnpm_executable is None:
        print("pnpm is required for M1 browser verification.", file=sys.stderr)
        return 127
    required_files = (
        COMPOSE_TEST_FILE,
        COMPOSE_M1_FILE,
        FIXTURE_PDF,
        FIXTURE_TEXT,
    )
    if any(not path.is_file() for path in required_files):
        print("A required M1 runtime verification file is unavailable.", file=sys.stderr)
        return 2

    run_id = uuid4().hex[:8]
    project_name = f"investment-research-m1-check-{run_id}"
    acceptance_image = f"investment-research-m1-acceptance:{run_id}"
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
        "--file",
        str(COMPOSE_M1_FILE),
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
                "INVESTMENT_RESEARCH_S3_BUCKET": f"m1-acceptance-{run_id}",
                "INVESTMENT_RESEARCH_S3_REGION_NAME": "us-east-1",
                "M1_ACCEPTANCE_IMAGE": acceptance_image,
                "M0_ACCEPTANCE_FIXTURE_PATH": str(FIXTURE_PDF),
                "M1_ACCEPTANCE_TEXT_FIXTURE_PATH": str(FIXTURE_TEXT),
            }
        )
        try:
            run_command(
                "Build and start isolated complete M1 runtime",
                [*compose_command, "up", "--build", "--detach", "--wait", "web", "worker"],
                environment=compose_environment,
            )
            run_command(
                "Seed isolated M1 PostgreSQL through the internal tool container",
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
                "Build isolated M1 acceptance image",
                [*compose_command, "build", "acceptance"],
                environment=compose_environment,
            )
            run_command(
                "Run automatic M1 PDF and text journey",
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
            if skip_browser:
                print(
                    "Browser journey explicitly skipped; full M1 gate remains incomplete.",
                    flush=True,
                )
            else:
                assert pnpm_executable is not None
                browser_environment = os.environ.copy()
                browser_environment["PLAYWRIGHT_BASE_URL"] = f"http://127.0.0.1:{web_port}"
                run_command(
                    "Run visible M1 Chromium journey",
                    [
                        pnpm_executable,
                        "--filter",
                        "@investment-research/frontend",
                        "test:browser",
                    ],
                    environment=browser_environment,
                    working_directory=REPOSITORY_ROOT,
                )
            runtime_logs = capture_runtime_logs(compose_command, compose_environment)
            assert_runtime_logs_are_safe(runtime_logs, secret_values)
            print("Automatic M1 PDF and text journey passed.", flush=True)
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
                "Remove isolated M1 runtime and its volumes",
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
    """Run the complete M1 runtime verification command."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-browser",
        action="store_true",
        help="Run the isolated HTTP journey and log checks without the separate browser gate.",
    )
    arguments = parser.parse_args()
    return verify_m1_runtime(skip_browser=arguments.skip_browser)


if __name__ == "__main__":
    raise SystemExit(main())
