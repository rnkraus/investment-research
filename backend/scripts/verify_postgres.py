"""Run the complete PostgreSQL verification against an isolated Docker database."""

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
POSTGRES_DATABASE = "investment_research"
POSTGRES_USER = "investment_research"


def find_docker_executable() -> str | None:
    """Return the Docker executable path when it is available."""

    return shutil.which("docker")


def generate_disposable_password() -> str:
    """Return a URL-safe random password used only for one test database."""

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
    working_directory: Path,
    environment: Mapping[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run one visible command without rendering environment secrets."""

    print(f"\n==> {label}", flush=True)
    print(f"$ {shlex.join(command)}", flush=True)
    return subprocess.run(
        command,
        cwd=working_directory,
        env=environment,
        check=check,
    )


def verify_postgres() -> int:
    """Create, verify, and always remove one isolated PostgreSQL environment."""

    if find_docker_executable() is None:
        print("Docker is required for PostgreSQL verification.", file=sys.stderr)
        return 127

    project_name = f"investment-research-postgres-check-{uuid4().hex[:8]}"
    postgres_port = find_available_loopback_port()
    secret_values = {name: generate_disposable_password() for name in COMPOSE_SECRET_FILES}
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
    with disposable_compose_secret_environment(secret_values) as secret_sources:
        compose_environment = os.environ.copy() | secret_sources
        compose_environment.update(
            {
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "POSTGRES_DB": POSTGRES_DATABASE,
                "POSTGRES_USER": POSTGRES_USER,
                "POSTGRES_PORT": str(postgres_port),
                "INVESTMENT_RESEARCH_S3_BUCKET": "investment-research-postgres-check",
            }
        )
        verification_environment = os.environ.copy()
        verification_environment.update(
            {
                "INVESTMENT_RESEARCH_ENVIRONMENT": "test",
                "INVESTMENT_RESEARCH_DATABASE_URL": (
                    f"postgresql+psycopg://{POSTGRES_USER}:{postgres_password}"
                    f"@127.0.0.1:{postgres_port}/{POSTGRES_DATABASE}"
                ),
                "INVESTMENT_RESEARCH_REDIS_URL": ("redis://:integration-test-only@127.0.0.1:1/0"),
            }
        )
        exit_code = 0

        try:
            run_command(
                "Start isolated PostgreSQL",
                [*compose_command, "up", "--detach", "--wait", "postgres"],
                working_directory=REPOSITORY_ROOT,
                environment=compose_environment,
            )
            run_command(
                "Migrate empty database to Alembic head",
                [*python_command, "alembic", "-c", "alembic.ini", "upgrade", "head"],
                working_directory=BACKEND_ROOT,
                environment=verification_environment,
            )
            run_command(
                "Seed deterministic M0.0 workspaces",
                [*python_command, "scripts.seed_m0_0"],
                working_directory=BACKEND_ROOT,
                environment=verification_environment,
            )
            run_command(
                "Run PostgreSQL integration tests",
                [*python_command, "pytest", "-q", "tests/integration"],
                working_directory=BACKEND_ROOT,
                environment=verification_environment,
            )
            run_command(
                "Check SQLAlchemy metadata against the migrated schema",
                [*python_command, "alembic", "-c", "alembic.ini", "check"],
                working_directory=BACKEND_ROOT,
                environment=verification_environment,
            )
            run_command(
                "Downgrade the disposable database to base",
                [*python_command, "alembic", "-c", "alembic.ini", "downgrade", "base"],
                working_directory=BACKEND_ROOT,
                environment=verification_environment,
            )
            run_command(
                "Upgrade the disposable database back to head",
                [*python_command, "alembic", "-c", "alembic.ini", "upgrade", "head"],
                working_directory=BACKEND_ROOT,
                environment=verification_environment,
            )
            run_command(
                "Recheck schema after the migration cycle",
                [*python_command, "alembic", "-c", "alembic.ini", "check"],
                working_directory=BACKEND_ROOT,
                environment=verification_environment,
            )
        except subprocess.CalledProcessError as error:
            exit_code = error.returncode or 1
        finally:
            cleanup = run_command(
                "Remove isolated PostgreSQL and its volume",
                [*compose_command, "down", "--volumes", "--remove-orphans"],
                working_directory=REPOSITORY_ROOT,
                environment=compose_environment,
                check=False,
            )
            if cleanup.returncode != 0 and exit_code == 0:
                exit_code = cleanup.returncode

    return exit_code


def main() -> int:
    """Run the PostgreSQL verification command."""

    return verify_postgres()


if __name__ == "__main__":
    raise SystemExit(main())
