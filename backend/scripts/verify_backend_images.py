"""Build and verify the backend container targets without using project secrets."""

import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.docker_secrets import (
    COMPOSE_SECRET_FILES,
    disposable_compose_secret_environment,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
DOCKERFILE = BACKEND_ROOT / "Dockerfile"
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
IMAGE_PREFIX = "investment-research-backend-check"
EXPECTED_USER = "65532:65532"
POSTGRES_DATABASE = "investment_research"
POSTGRES_USER = "investment_research"
API_HEALTH_TIMEOUT_SECONDS = 45
FORBIDDEN_CONFIGURATION_NAMES = (
    "INVESTMENT_RESEARCH_DATABASE_URL",
    "POSTGRES_PASSWORD",
    "MINIO_ROOT_PASSWORD",
    "REDIS_PASSWORD",
    "AWS_SECRET_ACCESS_KEY",
    "OPENAI_API_KEY",
    "PINECONE_API_KEY",
    "LANGSMITH_API_KEY",
)
RUNTIME_SMOKE_CODE = """\
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import investment_research

assert os.getuid() == 65532
assert os.getgid() == 65532
assert importlib.util.find_spec("pytest") is None
installer_check = (
    "import importlib.util; "
    "assert importlib.util.find_spec('pip') is None; "
    "assert importlib.util.find_spec('ensurepip') is None"
)
for executable in (sys.executable, str(Path(sys.base_prefix) / "bin/python3")):
    subprocess.run(
        [executable, "-I", "-c", installer_check],
        check=True,
        capture_output=True,
        timeout=10,
    )
for command in ("pip", "pip3", f"pip{sys.version_info.major}.{sys.version_info.minor}"):
    assert shutil.which(command) is None
assert not Path("/bin/uv").exists()
assert Path("/app/src/investment_research").is_dir()
assert not os.access("/app/src", os.W_OK)
assert not Path("/app/pyproject.toml").exists()
assert not Path("/app/uv.lock").exists()
assert investment_research.__version__ == "0.0.0"
"""
API_ENDPOINT_SMOKE_CODE = """\
import json
from urllib.request import urlopen

expectations = (
    (
        "/health",
        {
            "status": "ok",
            "service": "Investment Research Archive",
            "version": "0.0.0",
        },
    ),
    ("/ready", {"status": "ready"}),
)
for path, expected_body in expectations:
    with urlopen(f"http://127.0.0.1:8000{path}", timeout=2) as response:
        assert response.status == 200
        assert json.load(response) == expected_body
"""


@dataclass(frozen=True, slots=True)
class TargetExpectation:
    """Expected immutable runtime metadata for one named Docker target."""

    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    has_healthcheck: bool


TARGETS: dict[str, TargetExpectation] = {
    "api": TargetExpectation(
        entrypoint=("/app/.venv/bin/uvicorn",),
        command=(
            "investment_research.main:create_app",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--no-access-log",
            "--no-proxy-headers",
        ),
        has_healthcheck=True,
    ),
    "migration": TargetExpectation(
        entrypoint=("/app/.venv/bin/alembic",),
        command=("-c", "alembic.ini", "upgrade", "head"),
        has_healthcheck=False,
    ),
    "seed": TargetExpectation(
        entrypoint=("/app/.venv/bin/python", "-m", "scripts.seed_m0_0"),
        command=(),
        has_healthcheck=False,
    ),
    "worker": TargetExpectation(
        entrypoint=(
            "/app/.venv/bin/celery",
            "--app",
            "investment_research.worker:app",
            "worker",
        ),
        command=(
            "--loglevel=INFO",
            "--concurrency=1",
            "--without-gossip",
            "--without-mingle",
        ),
        has_healthcheck=True,
    ),
    "parser": TargetExpectation(
        entrypoint=(
            "/app/.venv/bin/python",
            "-m",
            "investment_research.parser_app",
        ),
        command=(),
        has_healthcheck=True,
    ),
}


class ImageVerificationError(RuntimeError):
    """Report a safe container invariant failure without image metadata."""


def find_docker_executable() -> str | None:
    """Return the Docker executable path when it is available."""

    return shutil.which("docker")


def run_command(
    label: str,
    command: Sequence[str],
    *,
    capture_output: bool = False,
    check: bool = True,
    environment: Mapping[str, str] | None = None,
    working_directory: Path = REPOSITORY_ROOT,
    visible: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one command without a shell and without rendering its environment."""

    if visible:
        print(f"\n==> {label}", flush=True)
        print(f"$ {shlex.join(command)}", flush=True)
    return subprocess.run(
        command,
        cwd=working_directory,
        env=environment,
        capture_output=capture_output,
        text=True,
        check=check,
    )


def load_image_configuration(image: str) -> dict[str, Any]:
    """Read one image configuration without printing its serialized content."""

    result = run_command(
        "Inspect image configuration",
        ["docker", "image", "inspect", image],
        capture_output=True,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        raise ImageVerificationError("Docker returned an unexpected image inspection result.")
    configuration = payload[0].get("Config")
    if not isinstance(configuration, dict):
        raise ImageVerificationError("Docker image configuration is missing.")
    return configuration


def load_image_history(image: str) -> str:
    """Read non-truncated build history without echoing it to verification output."""

    result = run_command(
        "Inspect image history",
        [
            "docker",
            "image",
            "history",
            "--no-trunc",
            "--format",
            "{{.CreatedBy}}",
            image,
        ],
        capture_output=True,
    )
    return result.stdout


def verify_configuration(
    configuration: dict[str, Any],
    history: str,
    expectation: TargetExpectation,
) -> None:
    """Enforce non-root, process-target, health, and secret-name invariants."""

    if configuration.get("User") != EXPECTED_USER:
        raise ImageVerificationError("Backend image does not declare the required non-root user.")
    if tuple(configuration.get("Entrypoint") or ()) != expectation.entrypoint:
        raise ImageVerificationError("Backend image entrypoint does not match its target.")
    if tuple(configuration.get("Cmd") or ()) != expectation.command:
        raise ImageVerificationError("Backend image command does not match its target.")

    has_healthcheck = isinstance(configuration.get("Healthcheck"), dict)
    if has_healthcheck != expectation.has_healthcheck:
        raise ImageVerificationError("Backend image healthcheck does not match its target.")

    serialized_metadata = f"{json.dumps(configuration, sort_keys=True)}\n{history}".upper()
    for forbidden_name in FORBIDDEN_CONFIGURATION_NAMES:
        if forbidden_name in serialized_metadata:
            raise ImageVerificationError(
                "Backend image metadata contains a forbidden configuration name."
            )


def verify_runtime(image: str) -> None:
    """Run a non-root import smoke test against the minimal runtime contents."""

    run_command(
        "Verify minimal non-root runtime",
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/app/.venv/bin/python",
            image,
            "-c",
            RUNTIME_SMOKE_CODE,
        ],
    )


def generate_disposable_password() -> str:
    """Return a random password used only by one isolated PostgreSQL instance."""

    return secrets.token_hex(24)


def sanitized_process_environment() -> dict[str, str]:
    """Copy the host environment without inherited secret-bearing configuration."""

    return {
        name: value
        for name, value in os.environ.items()
        if name not in FORBIDDEN_CONFIGURATION_NAMES
    }


def wait_for_healthy_container(container: str) -> None:
    """Wait for Docker's bounded API readiness check to report healthy."""

    deadline = time.monotonic() + API_HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = run_command(
            "Inspect API container health",
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            capture_output=True,
            visible=False,
        )
        status = result.stdout.strip()
        if status == "healthy":
            return
        if status == "unhealthy":
            raise ImageVerificationError("API container readiness check failed.")
        time.sleep(1)
    raise ImageVerificationError("API container readiness check timed out.")


def verify_api_against_migrated_postgres(
    api_image: str,
    migration_image: str,
    run_identifier: str,
) -> None:
    """Migrate PostgreSQL and start the API against its required dependencies."""

    project_name = f"investment-research-api-check-{run_identifier}"
    api_container = f"{project_name}-api"
    postgres_password = generate_disposable_password()
    secret_values = {name: generate_disposable_password() for name in COMPOSE_SECRET_FILES}
    secret_values["POSTGRES_PASSWORD"] = postgres_password
    api_environment = sanitized_process_environment()
    api_environment.update(
        {
            "INVESTMENT_RESEARCH_ENVIRONMENT": "test",
            "INVESTMENT_RESEARCH_RUNTIME_LOCATION": "container",
            "INVESTMENT_RESEARCH_API_HOST": "0.0.0.0",
            "INVESTMENT_RESEARCH_DATABASE_URL": (
                f"postgresql+psycopg://{POSTGRES_USER}:{postgres_password}"
                f"@postgres:5432/{POSTGRES_DATABASE}"
            ),
            "INVESTMENT_RESEARCH_REDIS_URL": (
                f"redis://:{secret_values['REDIS_PASSWORD']}@redis:6379/0"
            ),
            "INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT": "/tmp/object-storage",
        }
    )
    compose_command = [
        "docker",
        "compose",
        "--project-name",
        project_name,
        "--file",
        str(COMPOSE_FILE),
    ]
    with disposable_compose_secret_environment(secret_values) as secret_sources:
        compose_environment = sanitized_process_environment() | secret_sources
        compose_environment.update(
            {
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "POSTGRES_DB": POSTGRES_DATABASE,
                "POSTGRES_USER": POSTGRES_USER,
                "INVESTMENT_RESEARCH_S3_BUCKET": "unused-storage-check",
            }
        )
        migration_environment = sanitized_process_environment() | {
            "INVESTMENT_RESEARCH_ENVIRONMENT": "test",
            "INVESTMENT_RESEARCH_RUNTIME_LOCATION": "container",
            "POSTGRES_DB": POSTGRES_DATABASE,
            "POSTGRES_USER": POSTGRES_USER,
            "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_password",
            "POSTGRES_PORT": "5432",
        }
        migration_command = [
            "docker",
            "run",
            "--rm",
            "--network",
            f"{project_name}_backend",
            "--env",
            "INVESTMENT_RESEARCH_ENVIRONMENT",
            "--env",
            "INVESTMENT_RESEARCH_RUNTIME_LOCATION",
            "--env",
            "POSTGRES_DB",
            "--env",
            "POSTGRES_USER",
            "--env",
            "POSTGRES_PASSWORD_FILE",
            "--env",
            "POSTGRES_PORT",
            "--mount",
            (
                "type=bind,"
                f"source={secret_sources['POSTGRES_PASSWORD_SECRET_FILE']},"
                "target=/run/secrets/postgres_password,readonly"
            ),
            migration_image,
        ]
        api_created = False
        cleanup_failed = False

        try:
            run_command(
                "Start isolated PostgreSQL and Redis for the API image",
                [*compose_command, "up", "--detach", "--wait", "postgres", "redis"],
                environment=compose_environment,
            )
            run_command(
                "Migrate the isolated database to Alembic head",
                migration_command,
                environment=migration_environment,
            )
            run_command(
                "Verify migration image idempotency",
                migration_command,
                environment=migration_environment,
            )
            run_command(
                "Start the API image with runtime-only configuration",
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    api_container,
                    "--network",
                    f"{project_name}_backend",
                    "--env",
                    "INVESTMENT_RESEARCH_ENVIRONMENT",
                    "--env",
                    "INVESTMENT_RESEARCH_RUNTIME_LOCATION",
                    "--env",
                    "INVESTMENT_RESEARCH_API_HOST",
                    "--env",
                    "INVESTMENT_RESEARCH_DATABASE_URL",
                    "--env",
                    "INVESTMENT_RESEARCH_REDIS_URL",
                    "--env",
                    "INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,noexec,size=16m",
                    "--mount",
                    f"type=bind,source={REPOSITORY_ROOT / 'config' / 'rag.toml'},"
                    "target=/config/rag.toml,readonly",
                    api_image,
                ],
                capture_output=True,
                environment=api_environment,
            )
            api_created = True
            wait_for_healthy_container(api_container)
            run_command(
                "Verify layered API health endpoints",
                [
                    "docker",
                    "exec",
                    api_container,
                    "/app/.venv/bin/python",
                    "-c",
                    API_ENDPOINT_SMOKE_CODE,
                ],
            )
        finally:
            if api_created:
                cleanup = run_command(
                    "Remove the API verification container",
                    ["docker", "container", "rm", "--force", api_container],
                    check=False,
                )
                cleanup_failed = cleanup.returncode != 0
            cleanup = run_command(
                "Remove isolated API dependencies, networks, and volumes",
                [*compose_command, "down", "--volumes", "--remove-orphans"],
                environment=compose_environment,
                check=False,
            )
            cleanup_failed = cleanup.returncode != 0 or cleanup_failed

        if cleanup_failed:
            raise ImageVerificationError("API runtime verification cleanup failed.")


def verify_backend_images() -> int:
    """Build, inspect, exercise, and remove every backend target."""

    if find_docker_executable() is None:
        print("Docker is required for backend image verification.", file=sys.stderr)
        return 127

    run_identifier = uuid4().hex[:8]
    built_images: list[str] = []
    api_image: str | None = None
    migration_image: str | None = None
    exit_code = 0
    try:
        for target, expectation in TARGETS.items():
            image = f"{IMAGE_PREFIX}-{target}:{run_identifier}"
            run_command(
                f"Build backend {target} target",
                [
                    "docker",
                    "buildx",
                    "build",
                    "--quiet",
                    "--load",
                    "--target",
                    target,
                    "--tag",
                    image,
                    "--file",
                    str(DOCKERFILE),
                    str(BACKEND_ROOT),
                ],
            )
            built_images.append(image)
            if target == "api":
                api_image = image
            elif target == "migration":
                migration_image = image
            configuration = load_image_configuration(image)
            history = load_image_history(image)
            verify_configuration(configuration, history, expectation)
            verify_runtime(image)
        if api_image is None:
            raise ImageVerificationError("API image was not built.")
        if migration_image is None:
            raise ImageVerificationError("Migration image was not built.")
        verify_api_against_migrated_postgres(api_image, migration_image, run_identifier)
    except subprocess.CalledProcessError as error:
        exit_code = error.returncode or 1
    except (ImageVerificationError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        exit_code = 1
    finally:
        for image in reversed(built_images):
            cleanup = run_command(
                "Remove verification image",
                ["docker", "image", "rm", image],
                check=False,
            )
            if cleanup.returncode != 0 and exit_code == 0:
                exit_code = cleanup.returncode

    return exit_code


def main() -> int:
    """Run backend container verification."""

    return verify_backend_images()


if __name__ == "__main__":
    raise SystemExit(main())
