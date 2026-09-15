"""Verify the complete M0 fixture journey through the isolated runtime."""

import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

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


def find_docker_executable() -> str | None:
    """Return the Docker executable path when it is available."""

    return shutil.which("docker")


def generate_disposable_credential() -> str:
    """Return one random credential used only by an isolated runtime."""

    return secrets.token_hex(24)


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
) -> subprocess.CompletedProcess[bytes]:
    """Run one visible command without rendering its environment values."""

    print(f"\n==> {label}", flush=True)
    print(f"$ {shlex.join(command)}", flush=True)
    return subprocess.run(
        command,
        cwd=working_directory,
        env=environment,
        check=check,
    )


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
    secret_values = {name: generate_disposable_credential() for name in COMPOSE_SECRET_FILES}
    postgres_password = secret_values["POSTGRES_PASSWORD"]
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
    python_command = [sys.executable, "-m"]
    exit_code = 0

    with disposable_compose_secret_environment(secret_values) as secret_sources:
        compose_environment = os.environ.copy() | secret_sources
        compose_environment.update(
            {
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "POSTGRES_DB": POSTGRES_DATABASE,
                "POSTGRES_USER": POSTGRES_USER,
                "POSTGRES_PORT": str(postgres_port),
                "INVESTMENT_RESEARCH_S3_BUCKET": f"m0-acceptance-{run_id}",
                "INVESTMENT_RESEARCH_S3_REGION_NAME": "us-east-1",
                "M0_ACCEPTANCE_IMAGE": acceptance_image,
                "M0_ACCEPTANCE_FIXTURE_PATH": str(FIXTURE_PDF),
            }
        )
        migration_environment = os.environ.copy()
        migration_environment.update(
            {
                "INVESTMENT_RESEARCH_ENVIRONMENT": "test",
                "INVESTMENT_RESEARCH_DATABASE_URL": (
                    f"postgresql+psycopg://{POSTGRES_USER}:{postgres_password}"
                    f"@127.0.0.1:{postgres_port}/{POSTGRES_DATABASE}"
                ),
            }
        )

        try:
            run_command(
                "Build and start isolated M0 processing runtime",
                [*compose_command, "up", "--build", "--detach", "--wait", "worker"],
                environment=compose_environment,
            )
            run_command(
                "Migrate isolated PostgreSQL database",
                [*python_command, "alembic", "-c", "alembic.ini", "upgrade", "head"],
                environment=migration_environment,
                working_directory=BACKEND_ROOT,
            )
            run_command(
                "Seed isolated PostgreSQL workspaces",
                [*python_command, "scripts.seed_m0_0"],
                environment=migration_environment,
                working_directory=BACKEND_ROOT,
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
                    "-p",
                    "no:cacheprovider",
                    ACCEPTANCE_TEST,
                ],
                environment=compose_environment,
            )
            print("Automatic M0 fixture journey passed.", flush=True)
        except subprocess.CalledProcessError as error:
            exit_code = error.returncode or 1
            run_command(
                "Show isolated worker logs after verification failure",
                [*compose_command, "logs", "--no-color", "worker", "parser"],
                environment=compose_environment,
                check=False,
            )
        finally:
            cleanup = run_command(
                "Remove isolated M0 runtime and its volumes",
                [*compose_command, "down", "--volumes", "--remove-orphans"],
                environment=compose_environment,
                check=False,
            )
            if cleanup.returncode != 0 and exit_code == 0:
                exit_code = cleanup.returncode
            image_cleanup = run_command(
                "Remove isolated M0 acceptance image",
                ["docker", "image", "rm", "--force", acceptance_image],
                environment=compose_environment,
                check=False,
            )
            if image_cleanup.returncode != 0 and exit_code == 0:
                exit_code = image_cleanup.returncode

    return exit_code


def main() -> int:
    """Run the complete M0 runtime verification command."""

    return verify_m0_runtime()


if __name__ == "__main__":
    raise SystemExit(main())
