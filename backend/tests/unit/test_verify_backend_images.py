"""Unit tests for backend container verification orchestration."""

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

import pytest

from scripts import verify_backend_images

RUN_UUID = UUID("12345678-1234-4000-8000-123456789abc")


def image_configuration(target: str) -> dict[str, object]:
    """Return safe Docker metadata matching one expected target."""

    expectation = verify_backend_images.TARGETS[target]
    configuration: dict[str, object] = {
        "User": verify_backend_images.EXPECTED_USER,
        "Entrypoint": list(expectation.entrypoint),
        "Cmd": list(expectation.command),
        "Env": ["PYTHONUNBUFFERED=1"],
    }
    if expectation.has_healthcheck:
        configuration["Healthcheck"] = {"Test": ["CMD", "safe-readiness-check"]}
    return configuration


def target_from_image(image: str) -> str:
    """Resolve the target segment from a deterministic verification tag."""

    return image.removeprefix(f"{verify_backend_images.IMAGE_PREFIX}-").split(":", 1)[0]


def test_image_verification_builds_checks_and_cleans_every_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_backend_images, "find_docker_executable", lambda: "/usr/bin/docker")
    monkeypatch.setattr(verify_backend_images, "uuid4", lambda: RUN_UUID)
    calls: list[tuple[str, tuple[str, ...]]] = []
    api_checks: list[tuple[str, str, str]] = []

    def record_command(
        label: str,
        command: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        environment: Mapping[str, str] | None = None,
        working_directory: Path = verify_backend_images.REPOSITORY_ROOT,
        visible: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del check, environment, working_directory, visible
        calls.append((label, tuple(command)))
        stdout = ""
        if label == "Inspect image configuration":
            target = target_from_image(command[-1])
            stdout = json.dumps([{"Config": image_configuration(target)}])
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(verify_backend_images, "run_command", record_command)
    monkeypatch.setattr(
        verify_backend_images,
        "verify_api_against_migrated_postgres",
        lambda api_image, migration_image, run_identifier: api_checks.append(
            (api_image, migration_image, run_identifier)
        ),
    )

    assert verify_backend_images.verify_backend_images() == 0
    build_commands = [command for label, command in calls if label.startswith("Build backend")]
    assert [command[command.index("--target") + 1] for command in build_commands] == [
        "api",
        "migration",
        "worker",
        "parser",
    ]
    assert len([label for label, _ in calls if label == "Verify minimal non-root runtime"]) == 4
    assert api_checks == [
        (
            "investment-research-backend-check-api:12345678",
            "investment-research-backend-check-migration:12345678",
            "12345678",
        )
    ]
    cleanup_images = [
        command[-1] for label, command in calls if label == "Remove verification image"
    ]
    assert [target_from_image(image) for image in cleanup_images] == [
        "parser",
        "worker",
        "migration",
        "api",
    ]


def test_image_verification_cleans_up_after_an_invariant_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_backend_images, "find_docker_executable", lambda: "/usr/bin/docker")
    monkeypatch.setattr(verify_backend_images, "uuid4", lambda: RUN_UUID)
    labels: list[str] = []

    def return_root_image(
        label: str,
        command: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        environment: Mapping[str, str] | None = None,
        working_directory: Path = verify_backend_images.REPOSITORY_ROOT,
        visible: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, check, environment, working_directory, visible
        labels.append(label)
        stdout = json.dumps([{"Config": image_configuration("api") | {"User": "0:0"}}])
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(verify_backend_images, "run_command", return_root_image)

    assert verify_backend_images.verify_backend_images() == 1
    assert labels == [
        "Build backend api target",
        "Inspect image configuration",
        "Inspect image history",
        "Remove verification image",
    ]


def test_image_verification_rejects_secret_names_in_metadata() -> None:
    expectation = verify_backend_images.TARGETS["api"]

    with pytest.raises(
        verify_backend_images.ImageVerificationError,
        match="forbidden configuration name",
    ):
        verify_backend_images.verify_configuration(
            image_configuration("api"),
            "RUN export POSTGRES_PASSWORD=synthetic-marker",
            expectation,
        )


def test_api_runtime_verification_migrates_checks_and_cleans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[str, ...], Mapping[str, str] | None]] = []
    secret_paths: list[Path] = []
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_DATABASE_URL",
        "postgresql+psycopg://inherited:secret@ci-postgres:5432/inherited",
    )
    monkeypatch.setenv("POSTGRES_PASSWORD", "inherited-postgres-password")
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-provider-key")
    monkeypatch.setattr(
        verify_backend_images,
        "generate_disposable_password",
        lambda: "disposable-password",
    )

    def record_command(
        label: str,
        command: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        environment: Mapping[str, str] | None = None,
        working_directory: Path = verify_backend_images.REPOSITORY_ROOT,
        visible: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, check, working_directory, visible
        calls.append((label, tuple(command), environment))
        if label == "Start isolated PostgreSQL and Redis for the API image":
            assert environment is not None
            secret_path = Path(environment["POSTGRES_PASSWORD_SECRET_FILE"])
            assert secret_path.read_text(encoding="utf-8") == "disposable-password"
            secret_paths.append(secret_path)
        stdout = "healthy\n" if label == "Inspect API container health" else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(verify_backend_images, "run_command", record_command)

    verify_backend_images.verify_api_against_migrated_postgres(
        "investment-research-backend-check-api:test",
        "investment-research-backend-check-migration:test",
        "12345678",
    )

    labels = [label for label, _, _ in calls]
    assert labels == [
        "Start isolated PostgreSQL and Redis for the API image",
        "Migrate the isolated database to Alembic head",
        "Verify migration image idempotency",
        "Start the API image with runtime-only configuration",
        "Inspect API container health",
        "Verify layered API health endpoints",
        "Remove the API verification container",
        "Remove isolated API dependencies, networks, and volumes",
    ]
    dependency_command = next(
        command
        for label, command, _ in calls
        if label == "Start isolated PostgreSQL and Redis for the API image"
    )
    assert dependency_command[-2:] == ("postgres", "redis")
    api_command = next(
        command
        for label, command, _ in calls
        if label == "Start the API image with runtime-only configuration"
    )
    assert "disposable-password" not in api_command
    assert not any("postgresql+psycopg" in argument for argument in api_command)
    assert api_command.count("--env") == 6
    assert "INVESTMENT_RESEARCH_REDIS_URL" in api_command
    assert any(argument.endswith("12345678_backend") for argument in api_command)

    migration_command = next(
        command
        for label, command, _ in calls
        if label == "Migrate the isolated database to Alembic head"
    )
    assert migration_command[-1] == "investment-research-backend-check-migration:test"
    assert "disposable-password" not in migration_command
    assert not any("postgresql+psycopg" in argument for argument in migration_command)
    assert migration_command.count("--env") == 6
    assert any(
        argument.endswith("target=/run/secrets/postgres_password,readonly")
        for argument in migration_command
    )

    api_environment = next(
        environment
        for label, _, environment in calls
        if label == "Start the API image with runtime-only configuration"
    )
    assert api_environment is not None
    assert api_environment["INVESTMENT_RESEARCH_REDIS_URL"] == (
        "redis://:disposable-password@redis:6379/0"
    )
    assert "inherited" not in api_environment["INVESTMENT_RESEARCH_DATABASE_URL"]
    assert "POSTGRES_PASSWORD" not in api_environment
    assert "OPENAI_API_KEY" not in api_environment

    migration_environment = next(
        environment
        for label, _, environment in calls
        if label == "Migrate the isolated database to Alembic head"
    )
    assert migration_environment is not None
    assert "INVESTMENT_RESEARCH_DATABASE_URL" not in migration_environment
    assert migration_environment["INVESTMENT_RESEARCH_RUNTIME_LOCATION"] == "container"
    assert migration_environment["POSTGRES_DB"] == "investment_research"
    assert migration_environment["POSTGRES_USER"] == "investment_research"
    assert migration_environment["POSTGRES_PASSWORD_FILE"] == "/run/secrets/postgres_password"
    assert migration_environment["POSTGRES_PORT"] == "5432"
    assert "POSTGRES_PASSWORD" not in migration_environment
    assert "OPENAI_API_KEY" not in migration_environment
    assert "unused-application-access" not in api_command
    assert "unused-application-secret" not in api_command
    assert secret_paths and all(not path.exists() for path in secret_paths)


def test_api_runtime_verification_removes_postgres_after_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels: list[str] = []
    monkeypatch.setattr(
        verify_backend_images,
        "generate_disposable_password",
        lambda: "disposable-password",
    )

    def fail_api_start(
        label: str,
        command: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        environment: Mapping[str, str] | None = None,
        working_directory: Path = verify_backend_images.REPOSITORY_ROOT,
        visible: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, check, environment, working_directory, visible
        labels.append(label)
        if label == "Start the API image with runtime-only configuration":
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(verify_backend_images, "run_command", fail_api_start)

    with pytest.raises(subprocess.CalledProcessError):
        verify_backend_images.verify_api_against_migrated_postgres(
            "investment-research-backend-check-api:test",
            "investment-research-backend-check-migration:test",
            "12345678",
        )

    assert "Remove the API verification container" not in labels
    assert labels[-1] == "Remove isolated API dependencies, networks, and volumes"
