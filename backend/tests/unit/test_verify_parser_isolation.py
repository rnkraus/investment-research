"""Unit tests for parser-isolation verification orchestration."""

import subprocess
from collections.abc import Mapping, Sequence
from uuid import UUID

import pytest

from scripts import verify_parser_isolation
from scripts.docker_secrets import COMPOSE_SECRET_FILES

RUN_UUID = UUID("12345678-1234-4000-8000-123456789abc")
PROJECT_NAME = "investment-research-parser-check-12345678"
CREDENTIAL = "synthetic-disposable-credential"


def parser_inspection() -> dict[str, object]:
    """Return minimal safe Docker metadata for the isolated parser."""

    return {
        "Config": {"User": "65532:65532"},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PortBindings": {},
            "Memory": verify_parser_isolation.PARSER_MEMORY_LIMIT_BYTES,
            "NanoCpus": verify_parser_isolation.PARSER_NANO_CPUS,
            "PidsLimit": verify_parser_isolation.PARSER_PID_LIMIT,
        },
        "Mounts": [],
        "NetworkSettings": {"Networks": {f"{PROJECT_NAME}_parser": {}}},
    }


def redis_inspection() -> dict[str, object]:
    """Return minimal safe Docker metadata for the internal Redis broker."""

    return {
        "Config": {"User": "999:1000"},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PortBindings": {},
        },
        "NetworkSettings": {
            "Networks": {f"{PROJECT_NAME}_backend": {}, f"{PROJECT_NAME}_repair": {}}
        },
    }


@pytest.mark.parametrize(
    "networks",
    [
        {f"{PROJECT_NAME}_backend": {}},
        {f"{PROJECT_NAME}_repair": {}},
        {f"{PROJECT_NAME}_backend": {}, f"{PROJECT_NAME}_repair": {}, "unexpected": {}},
    ],
)
def test_redis_metadata_requires_exact_backend_and_broker_only_repair_networks(
    networks: dict[str, object],
) -> None:
    metadata = redis_inspection()
    metadata["NetworkSettings"] = {"Networks": networks}
    with pytest.raises(verify_parser_isolation.ParserIsolationVerificationError):
        verify_parser_isolation.verify_redis_metadata(
            metadata, project_name=PROJECT_NAME, credentials=()
        )


def test_verification_builds_exercises_and_cleans_isolated_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_parser_isolation, "find_docker_executable", lambda: "/bin/docker")
    monkeypatch.setattr(verify_parser_isolation, "uuid4", lambda: RUN_UUID)
    monkeypatch.setattr(
        verify_parser_isolation,
        "generate_disposable_credential",
        lambda: CREDENTIAL,
    )
    commands: list[tuple[str, tuple[str, ...]]] = []
    runtime_checks: list[str] = []

    def record_command(
        label: str,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del environment, check
        commands.append((label, tuple(command)))
        return subprocess.CompletedProcess(command, 0)

    def return_inspection(
        compose_command: Sequence[str],
        service: str,
        *,
        environment: Mapping[str, str],
    ) -> Mapping[str, object]:
        del compose_command, environment
        return parser_inspection() if service == "parser" else redis_inspection()

    monkeypatch.setattr(verify_parser_isolation, "run_command", record_command)
    monkeypatch.setattr(verify_parser_isolation, "inspect_service", return_inspection)
    monkeypatch.setattr(
        verify_parser_isolation,
        "verify_parser_runtime",
        lambda *args, **kwargs: runtime_checks.append("runtime"),
    )
    monkeypatch.setattr(
        verify_parser_isolation,
        "verify_worker_parser_protocol",
        lambda *args, **kwargs: runtime_checks.append("protocol"),
    )

    assert verify_parser_isolation.verify_parser_isolation() == 0
    assert [label for label, _ in commands] == [
        "Build Redis, worker, and parser images",
        "Start internal Redis and parser services",
        "Remove isolated parser verification project",
    ]
    assert runtime_checks == ["runtime", "protocol"]
    assert all(CREDENTIAL not in argument for _, command in commands for argument in command)
    assert commands[-1][1][-3:] == ("down", "--volumes", "--remove-orphans")


def test_verification_cleans_up_after_start_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_parser_isolation, "find_docker_executable", lambda: "/bin/docker")
    monkeypatch.setattr(verify_parser_isolation, "uuid4", lambda: RUN_UUID)
    labels: list[str] = []

    def fail_start(
        label: str,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del environment, check
        labels.append(label)
        if label == "Start internal Redis and parser services":
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verify_parser_isolation, "run_command", fail_start)

    assert verify_parser_isolation.verify_parser_isolation() == 1
    assert labels[-1] == "Remove isolated parser verification project"


def test_parser_metadata_accepts_exact_isolated_runtime() -> None:
    verify_parser_isolation.verify_parser_metadata(
        parser_inspection(),
        project_name=PROJECT_NAME,
        credentials=(CREDENTIAL,),
    )


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        pytest.param("Memory", 0, "memory limit", id="memory"),
        pytest.param("NanoCpus", 0, "CPU limit", id="cpu"),
        pytest.param("PidsLimit", 0, "PID limit", id="pids"),
    ],
)
def test_parser_metadata_rejects_wrong_resource_limit(
    field: str,
    value: int,
    expected_error: str,
) -> None:
    inspection = parser_inspection()
    host_config = inspection["HostConfig"]
    assert isinstance(host_config, dict)
    host_config[field] = value

    with pytest.raises(
        verify_parser_isolation.ParserIsolationVerificationError,
        match=expected_error,
    ):
        verify_parser_isolation.verify_parser_metadata(
            inspection,
            project_name=PROJECT_NAME,
            credentials=(CREDENTIAL,),
        )


def test_parser_metadata_rejects_backend_network_membership() -> None:
    inspection = parser_inspection()
    network_settings = inspection["NetworkSettings"]
    assert isinstance(network_settings, dict)
    network_settings["Networks"] = {
        f"{PROJECT_NAME}_parser": {},
        f"{PROJECT_NAME}_backend": {},
    }

    with pytest.raises(
        verify_parser_isolation.ParserIsolationVerificationError,
        match="network membership",
    ):
        verify_parser_isolation.verify_parser_metadata(
            inspection,
            project_name=PROJECT_NAME,
            credentials=(CREDENTIAL,),
        )


def test_parser_metadata_rejects_bind_mount() -> None:
    inspection = parser_inspection()
    inspection["Mounts"] = [{"Type": "bind", "Destination": "/workspace"}]

    with pytest.raises(
        verify_parser_isolation.ParserIsolationVerificationError,
        match="bind or volume mount",
    ):
        verify_parser_isolation.verify_parser_metadata(
            inspection,
            project_name=PROJECT_NAME,
            credentials=(CREDENTIAL,),
        )


def test_parser_metadata_rejects_credential_value() -> None:
    inspection = parser_inspection()
    config = inspection["Config"]
    assert isinstance(config, dict)
    config["Env"] = [f"FORBIDDEN={CREDENTIAL}"]

    with pytest.raises(
        verify_parser_isolation.ParserIsolationVerificationError,
        match="credential value",
    ):
        verify_parser_isolation.verify_parser_metadata(
            inspection,
            project_name=PROJECT_NAME,
            credentials=(CREDENTIAL,),
        )


def test_redis_metadata_accepts_internal_secret_file_runtime() -> None:
    verify_parser_isolation.verify_redis_metadata(
        redis_inspection(),
        project_name=PROJECT_NAME,
        credentials=(CREDENTIAL,),
    )


def test_build_environment_disables_dotenv_and_uses_only_secret_source_paths() -> None:
    secret_sources = {
        source_name: f"/temporary/{filename}"
        for source_name, filename in COMPOSE_SECRET_FILES.values()
    }

    environment = verify_parser_isolation.build_environment(secret_sources)

    assert environment["COMPOSE_DISABLE_ENV_FILE"] == "1"
    assert environment.items() >= secret_sources.items()
    assert "POSTGRES_PASSWORD" not in environment
    assert "REDIS_PASSWORD" not in environment
    assert "INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID" not in environment
    assert "INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY" not in environment
    assert environment["INVESTMENT_RESEARCH_MAX_UPLOAD_BYTES"] == str(
        verify_parser_isolation.PARSER_REQUEST_LIMIT_BYTES
    )
