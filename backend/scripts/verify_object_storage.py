"""Verify SeaweedFS through one isolated and disposable Docker Compose project."""

import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import cast
from uuid import uuid4

from scripts.docker_secrets import disposable_compose_secret_environment

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
COMPOSE_TEST_FILE = REPOSITORY_ROOT / "compose.test.yaml"
INTEGRATION_TEST = "tests/integration/test_s3_object_storage_seaweedfs.py"
API_CONTRACT_TEST_NAMES = (
    "test_api_capture_and_authorized_original_open_use_seaweedfs",
    "test_api_text_capture_and_original_open_use_seaweedfs",
)
API_CONTRACT_TESTS = tuple(
    f"{INTEGRATION_TEST}::{test_name}" for test_name in API_CONTRACT_TEST_NAMES
)
STORAGE_SERVICES = (
    "seaweedfs-master",
    "seaweedfs-volume",
    "seaweedfs-filer",
    "seaweedfs-s3",
)
CONTROL_SERVICES = STORAGE_SERVICES[:-1]
HEALTH_TIMEOUT_SECONDS = 120.0
POLL_INTERVAL_SECONDS = 0.5
PROBE_IMAGE = "investment-research-backend-storage-bootstrap:local"
POSTGRES_DATABASE = "investment_research"
POSTGRES_USER = "investment_research"
S3_VERIFICATION_ENVIRONMENT_NAMES = (
    "INVESTMENT_RESEARCH_OBJECT_STORAGE_INTEGRATION",
    "INVESTMENT_RESEARCH_OBJECT_STORAGE_PERSISTENCE_PHASE",
    "INVESTMENT_RESEARCH_S3_ENDPOINT_URL",
    "INVESTMENT_RESEARCH_S3_REGION_NAME",
    "INVESTMENT_RESEARCH_S3_BUCKET",
    "INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID",
    "INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY",
)
API_CONTRACT_ENVIRONMENT_NAMES = (
    *S3_VERIFICATION_ENVIRONMENT_NAMES,
    "INVESTMENT_RESEARCH_ENVIRONMENT",
    "INVESTMENT_RESEARCH_RUNTIME_LOCATION",
    "INVESTMENT_RESEARCH_DATABASE_URL",
    "INVESTMENT_RESEARCH_REDIS_URL",
    "INVESTMENT_RESEARCH_OBJECT_STORAGE_PROVIDER",
)


class ObjectStorageVerificationError(RuntimeError):
    """Report a failed runtime contract without exposing disposable credentials."""


def find_docker_executable() -> str | None:
    """Return the Docker executable path when it is available."""

    return shutil.which("docker")


def generate_disposable_credential() -> str:
    """Return a shell-, JSON-, and URL-safe credential used by one test project."""

    return secrets.token_hex(24)


def find_available_loopback_port() -> int:
    """Reserve an available loopback port long enough to choose a disposable mapping."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def run_command(
    label: str,
    command: Sequence[str],
    *,
    working_directory: Path,
    environment: Mapping[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one visible command without rendering environment credentials."""

    print(f"\n==> {label}", flush=True)
    print(f"$ {shlex.join(command)}", flush=True)
    return subprocess.run(
        command,
        cwd=working_directory,
        env=environment,
        check=check,
        text=True,
    )


def capture_command(
    command: Sequence[str],
    *,
    working_directory: Path,
    environment: Mapping[str, str],
) -> str:
    """Capture machine-readable Docker state without printing its potentially noisy JSON."""

    result = subprocess.run(
        command,
        cwd=working_directory,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def compose_container_id(
    compose_command: Sequence[str],
    service: str,
    *,
    environment: Mapping[str, str],
) -> str:
    """Resolve exactly one container owned by the isolated Compose project."""

    container_id = capture_command(
        [*compose_command, "ps", "--all", "--quiet", service],
        working_directory=REPOSITORY_ROOT,
        environment=environment,
    )
    if not container_id or "\n" in container_id:
        raise ObjectStorageVerificationError(f"expected exactly one {service} container")
    return container_id


def inspect_container_state(
    container_id: str,
    *,
    environment: Mapping[str, str],
) -> Mapping[str, object]:
    """Return one container's state from Docker's machine-readable inspection output."""

    raw_state = capture_command(
        ["docker", "inspect", "--format", "{{json .State}}", container_id],
        working_directory=REPOSITORY_ROOT,
        environment=environment,
    )
    state = json.loads(raw_state)
    if not isinstance(state, dict):
        raise ObjectStorageVerificationError("Docker returned an invalid container state")
    return cast(Mapping[str, object], state)


def wait_for_healthy_service(
    compose_command: Sequence[str],
    service: str,
    *,
    environment: Mapping[str, str],
    timeout_seconds: float = HEALTH_TIMEOUT_SECONDS,
) -> None:
    """Wait until one long-running service is healthy or fails decisively."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        container_id = compose_container_id(compose_command, service, environment=environment)
        state = inspect_container_state(container_id, environment=environment)
        health = state.get("Health")
        if isinstance(health, dict) and health.get("Status") == "healthy":
            return
        if state.get("Status") in {"dead", "exited"}:
            raise ObjectStorageVerificationError(f"{service} stopped before becoming healthy")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise ObjectStorageVerificationError(f"{service} did not become healthy before timeout")


def wait_for_successful_bootstrap(
    compose_command: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout_seconds: float = HEALTH_TIMEOUT_SECONDS,
) -> None:
    """Wait for the one-shot bucket bootstrap to exit successfully."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        container_id = compose_container_id(
            compose_command,
            "seaweedfs-bootstrap",
            environment=environment,
        )
        state = inspect_container_state(container_id, environment=environment)
        if state.get("Status") == "exited":
            if state.get("ExitCode") == 0:
                return
            raise ObjectStorageVerificationError("SeaweedFS bucket bootstrap failed")
        if state.get("Status") == "dead":
            raise ObjectStorageVerificationError("SeaweedFS bucket bootstrap died")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise ObjectStorageVerificationError("SeaweedFS bucket bootstrap did not finish before timeout")


def inspect_container(
    compose_command: Sequence[str],
    service: str,
    *,
    environment: Mapping[str, str],
) -> Mapping[str, object]:
    """Return one complete container inspection object."""

    container_id = compose_container_id(compose_command, service, environment=environment)
    raw_inspection = capture_command(
        ["docker", "inspect", container_id],
        working_directory=REPOSITORY_ROOT,
        environment=environment,
    )
    inspection = json.loads(raw_inspection)
    if not isinstance(inspection, list) or len(inspection) != 1:
        raise ObjectStorageVerificationError("Docker returned an invalid container inspection")
    item = inspection[0]
    if not isinstance(item, dict):
        raise ObjectStorageVerificationError("Docker returned an invalid container inspection")
    return cast(Mapping[str, object], item)


def require_mapping(parent: Mapping[str, object], name: str) -> Mapping[str, object]:
    """Read one expected mapping from untrusted Docker inspection JSON."""

    value = parent.get(name)
    if not isinstance(value, dict):
        raise ObjectStorageVerificationError(f"Docker inspection is missing {name}")
    return cast(Mapping[str, object], value)


def verify_runtime_metadata(
    compose_command: Sequence[str],
    *,
    environment: Mapping[str, str],
    credentials: Sequence[str],
) -> None:
    """Verify non-root execution, confinement, port scope, and secret-file delivery."""

    for service in (*STORAGE_SERVICES, "seaweedfs-bootstrap"):
        inspection = inspect_container(compose_command, service, environment=environment)
        config = require_mapping(inspection, "Config")
        host_config = require_mapping(inspection, "HostConfig")
        if config.get("User") != "65532:65532":
            raise ObjectStorageVerificationError(
                f"{service} does not run as the fixed non-root user"
            )
        if host_config.get("CapDrop") != ["ALL"]:
            raise ObjectStorageVerificationError(f"{service} does not drop all Linux capabilities")
        security_options = host_config.get("SecurityOpt")
        if not isinstance(security_options, list) or not any(
            "no-new-privileges" in str(option) for option in security_options
        ):
            raise ObjectStorageVerificationError(f"{service} lacks no-new-privileges")
        expected_read_only = service in CONTROL_SERVICES
        if host_config.get("ReadonlyRootfs") is not expected_read_only:
            raise ObjectStorageVerificationError(
                f"{service} has an unexpected root-filesystem mode"
            )
        serialized = json.dumps(inspection, sort_keys=True)
        if any(credential in serialized for credential in credentials):
            raise ObjectStorageVerificationError(
                f"{service} exposes a credential in Docker metadata"
            )

    for service in STORAGE_SERVICES:
        inspection = inspect_container(compose_command, service, environment=environment)
        storage_host_config = require_mapping(inspection, "HostConfig")
        port_bindings = storage_host_config.get("PortBindings")
        if isinstance(port_bindings, dict) and any(port_bindings.values()):
            raise ObjectStorageVerificationError(f"{service} publishes a host port")


def verify_data_network_isolation(
    project_name: str,
    *,
    environment: Mapping[str, str],
) -> None:
    """Prove that a data-plane peer can reach S3 but not SeaweedFS control services."""

    probe = """
import socket

socket.create_connection(("seaweedfs-s3", 8333), timeout=3).close()
for host, port in (
    ("seaweedfs-master", 9333),
    ("seaweedfs-volume", 8080),
    ("seaweedfs-filer", 8888),
):
    try:
        socket.create_connection((host, port), timeout=1).close()
    except OSError:
        continue
    raise SystemExit(f"data network unexpectedly reached {host}:{port}")
""".strip()
    run_command(
        "Verify object-storage data-network isolation",
        [
            "docker",
            "run",
            "--rm",
            "--network",
            f"{project_name}_storage-data",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            PROBE_IMAGE,
            "/app/.venv/bin/python",
            "-c",
            probe,
        ],
        working_directory=REPOSITORY_ROOT,
        environment=environment,
    )


def verification_container_command(
    *,
    image: str,
    project_name: str,
    pytest_arguments: Sequence[str],
) -> list[str]:
    """Build one confined test-container command without credential values."""

    environment_arguments = [
        argument for name in S3_VERIFICATION_ENVIRONMENT_NAMES for argument in ("--env", name)
    ]
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        f"{project_name}_storage-data",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=128",
        "--memory=512m",
        "--cpus=1",
        *environment_arguments,
        image,
        "-p",
        "no:cacheprovider",
        *pytest_arguments,
    ]


def run_api_contract_test(
    *,
    image: str,
    project_name: str,
    environment: Mapping[str, str],
) -> None:
    """Run the API contract in one container connected only to its two data planes."""

    container_name = f"{project_name}-api-contract"
    environment_arguments = [
        argument for name in API_CONTRACT_ENVIRONMENT_NAMES for argument in ("--env", name)
    ]
    test_exit_code = 0
    try:
        run_command(
            "Create isolated API object-storage contract container",
            [
                "docker",
                "create",
                "--name",
                container_name,
                "--network",
                f"{project_name}_storage-data",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--pids-limit=128",
                "--memory=512m",
                "--cpus=1",
                "--mount",
                f"type=bind,source={REPOSITORY_ROOT / 'config' / 'rag.toml'},"
                "target=/config/rag.toml,readonly",
                *environment_arguments,
                image,
                "-p",
                "no:cacheprovider",
                "-q",
                *API_CONTRACT_TESTS,
            ],
            working_directory=REPOSITORY_ROOT,
            environment=environment,
        )
        run_command(
            "Connect API contract container to isolated PostgreSQL",
            [
                "docker",
                "network",
                "connect",
                f"{project_name}_backend",
                container_name,
            ],
            working_directory=REPOSITORY_ROOT,
            environment=environment,
        )
        run_command(
            "Run API capture and authorized original-open contract",
            ["docker", "start", "--attach", container_name],
            working_directory=REPOSITORY_ROOT,
            environment=environment,
        )
    except subprocess.CalledProcessError as error:
        test_exit_code = error.returncode or 1
    finally:
        cleanup = run_command(
            "Remove isolated API object-storage contract container",
            ["docker", "container", "rm", "--force", container_name],
            working_directory=REPOSITORY_ROOT,
            environment=environment,
            check=False,
        )
        if cleanup.returncode != 0 and test_exit_code == 0:
            test_exit_code = cleanup.returncode
    if test_exit_code != 0:
        raise subprocess.CalledProcessError(test_exit_code, ["docker", "start", container_name])


def verify_object_storage() -> int:
    """Create, verify, restart, and always remove one isolated SeaweedFS environment."""

    if find_docker_executable() is None:
        print("Docker is required for object-storage verification.", file=sys.stderr)
        return 127

    run_id = uuid4().hex[:8]
    project_name = f"investment-research-storage-check-{run_id}"
    verification_image = f"investment-research-object-storage-verification:{run_id}"
    bucket = f"investment-research-check-{run_id}"
    postgres_port = find_available_loopback_port()
    postgres_password = generate_disposable_credential()
    redis_password = generate_disposable_credential()
    admin_access_key = generate_disposable_credential()
    admin_secret_access_key = generate_disposable_credential()
    application_access_key = generate_disposable_credential()
    application_secret_access_key = generate_disposable_credential()
    secret_values = {
        "POSTGRES_PASSWORD": postgres_password,
        "REDIS_PASSWORD": redis_password,
        "SEAWEEDFS_ADMIN_ACCESS_KEY": admin_access_key,
        "SEAWEEDFS_ADMIN_SECRET_ACCESS_KEY": admin_secret_access_key,
        "INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID": application_access_key,
        "INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY": application_secret_access_key,
    }
    credentials = tuple(secret_values.values())
    compose_environment = os.environ.copy()
    secret_resources = ExitStack()
    python_command = [sys.executable, "-m"]
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

    try:
        secret_sources = secret_resources.enter_context(
            disposable_compose_secret_environment(secret_values)
        )
        compose_environment.update(
            {
                **secret_sources,
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "POSTGRES_DB": POSTGRES_DATABASE,
                "POSTGRES_USER": POSTGRES_USER,
                "POSTGRES_PORT": str(postgres_port),
                "INVESTMENT_RESEARCH_S3_BUCKET": bucket,
                "INVESTMENT_RESEARCH_S3_REGION_NAME": "us-east-1",
            }
        )
        verification_environment = compose_environment | {
            "INVESTMENT_RESEARCH_OBJECT_STORAGE_INTEGRATION": "1",
            "INVESTMENT_RESEARCH_OBJECT_STORAGE_PERSISTENCE_PHASE": "unused",
            "INVESTMENT_RESEARCH_ENVIRONMENT": "test",
            "INVESTMENT_RESEARCH_RUNTIME_LOCATION": "container",
            "INVESTMENT_RESEARCH_DATABASE_URL": (
                f"postgresql+psycopg://{POSTGRES_USER}:{postgres_password}"
                f"@postgres:5432/{POSTGRES_DATABASE}"
            ),
            "INVESTMENT_RESEARCH_REDIS_URL": f"redis://:{redis_password}@redis:6379/0",
            "INVESTMENT_RESEARCH_OBJECT_STORAGE_PROVIDER": "s3",
            "INVESTMENT_RESEARCH_S3_ENDPOINT_URL": "http://seaweedfs-s3:8333",
            "INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID": application_access_key,
            "INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY": application_secret_access_key,
        }
        migration_environment = compose_environment | {
            "INVESTMENT_RESEARCH_DATABASE_URL": (
                f"postgresql+psycopg://{POSTGRES_USER}:{postgres_password}"
                f"@127.0.0.1:{postgres_port}/{POSTGRES_DATABASE}"
            )
        }
        run_command(
            "Build object-storage verification image",
            [
                "docker",
                "build",
                "--file",
                str(BACKEND_ROOT / "Dockerfile"),
                "--target",
                "verification",
                "--tag",
                verification_image,
                str(BACKEND_ROOT),
            ],
            working_directory=REPOSITORY_ROOT,
            environment=compose_environment,
        )
        run_command(
            "Build and start isolated PostgreSQL, Redis, and SeaweedFS",
            [
                *compose_command,
                "up",
                "--build",
                "--detach",
                "postgres",
                "redis",
                "seaweedfs-bootstrap",
            ],
            working_directory=REPOSITORY_ROOT,
            environment=compose_environment,
        )
        wait_for_healthy_service(compose_command, "postgres", environment=compose_environment)
        wait_for_healthy_service(compose_command, "redis", environment=compose_environment)
        for service in STORAGE_SERVICES:
            wait_for_healthy_service(compose_command, service, environment=compose_environment)
        wait_for_successful_bootstrap(compose_command, environment=compose_environment)
        run_command(
            "Migrate isolated PostgreSQL database",
            [*python_command, "alembic", "-c", "alembic.ini", "upgrade", "head"],
            working_directory=BACKEND_ROOT,
            environment=migration_environment,
        )
        run_command(
            "Seed isolated PostgreSQL workspaces",
            [*python_command, "scripts.seed_m0_0"],
            working_directory=BACKEND_ROOT,
            environment=migration_environment,
        )
        verify_runtime_metadata(
            compose_command,
            environment=compose_environment,
            credentials=credentials,
        )
        verify_data_network_isolation(project_name, environment=compose_environment)
        run_command(
            "Verify idempotent bucket bootstrap",
            [*compose_command, "run", "--rm", "--no-deps", "seaweedfs-bootstrap"],
            working_directory=REPOSITORY_ROOT,
            environment=compose_environment,
        )
        run_command(
            "Run SeaweedFS S3 contract tests",
            verification_container_command(
                image=verification_image,
                project_name=project_name,
                pytest_arguments=(
                    "-q",
                    INTEGRATION_TEST,
                    "-k",
                    " and ".join(
                        (
                            "not test_object_survives_storage_restart",
                            *(f"not {test_name}" for test_name in API_CONTRACT_TEST_NAMES),
                        )
                    ),
                ),
            ),
            working_directory=REPOSITORY_ROOT,
            environment=verification_environment,
        )
        run_api_contract_test(
            image=verification_image,
            project_name=project_name,
            environment=verification_environment,
        )
        run_command(
            "Write restart-persistence probe object",
            verification_container_command(
                image=verification_image,
                project_name=project_name,
                pytest_arguments=(
                    "-q",
                    f"{INTEGRATION_TEST}::test_object_survives_storage_restart",
                ),
            ),
            working_directory=REPOSITORY_ROOT,
            environment=verification_environment
            | {"INVESTMENT_RESEARCH_OBJECT_STORAGE_PERSISTENCE_PHASE": "write"},
        )
        run_command(
            "Stop isolated SeaweedFS data path",
            [*compose_command, "stop", *reversed(STORAGE_SERVICES)],
            working_directory=REPOSITORY_ROOT,
            environment=compose_environment,
        )
        for service in STORAGE_SERVICES:
            run_command(
                f"Restart {service}",
                [*compose_command, "start", service],
                working_directory=REPOSITORY_ROOT,
                environment=compose_environment,
            )
            wait_for_healthy_service(compose_command, service, environment=compose_environment)
        run_command(
            "Read restart-persistence probe object",
            verification_container_command(
                image=verification_image,
                project_name=project_name,
                pytest_arguments=(
                    "-q",
                    f"{INTEGRATION_TEST}::test_object_survives_storage_restart",
                ),
            ),
            working_directory=REPOSITORY_ROOT,
            environment=verification_environment
            | {"INVESTMENT_RESEARCH_OBJECT_STORAGE_PERSISTENCE_PHASE": "read"},
        )
    except (ObjectStorageVerificationError, subprocess.CalledProcessError) as error:
        if isinstance(error, ObjectStorageVerificationError):
            print(f"Object-storage verification failed: {error}", file=sys.stderr)
            exit_code = 1
        else:
            exit_code = error.returncode or 1
    finally:
        cleanup = run_command(
            "Remove isolated SeaweedFS project and its volumes",
            [*compose_command, "down", "--volumes", "--remove-orphans"],
            working_directory=REPOSITORY_ROOT,
            environment=compose_environment,
            check=False,
        )
        if cleanup.returncode != 0 and exit_code == 0:
            exit_code = cleanup.returncode
        image_cleanup = run_command(
            "Remove isolated object-storage verification image",
            ["docker", "image", "rm", "--force", verification_image],
            working_directory=REPOSITORY_ROOT,
            environment=compose_environment,
            check=False,
        )
        if image_cleanup.returncode != 0 and exit_code == 0:
            exit_code = image_cleanup.returncode
        secret_resources.close()

    return exit_code


def main() -> int:
    """Run the object-storage verification command."""

    return verify_object_storage()


if __name__ == "__main__":
    raise SystemExit(main())
