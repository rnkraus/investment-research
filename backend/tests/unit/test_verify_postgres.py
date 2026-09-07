"""Unit tests for isolated PostgreSQL verification orchestration."""

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

import pytest

from scripts import verify_postgres

PROJECT_UUID = UUID("12345678-1234-4000-8000-123456789abc")
TEST_PASSWORD = "disposable-password"


def prepare_deterministic_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace nondeterministic discovery without executing external commands."""

    monkeypatch.setattr(
        verify_postgres,
        "find_docker_executable",
        lambda: "/usr/local/bin/docker",
    )
    monkeypatch.setattr(verify_postgres, "uuid4", lambda: PROJECT_UUID)
    monkeypatch.setattr(verify_postgres, "find_available_loopback_port", lambda: 55432)
    monkeypatch.setattr(
        verify_postgres,
        "generate_disposable_password",
        lambda: TEST_PASSWORD,
    )


def test_verification_runs_complete_sequence_without_exposing_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_deterministic_runtime(monkeypatch)
    calls: list[tuple[str, tuple[str, ...], Mapping[str, str]]] = []

    def record_command(
        label: str,
        command: Sequence[str],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        del working_directory, check
        calls.append((label, tuple(command), environment))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verify_postgres, "run_command", record_command)

    result = verify_postgres.verify_postgres()

    assert result == 0
    assert [label for label, _, _ in calls] == [
        "Start isolated PostgreSQL",
        "Migrate empty database to Alembic head",
        "Seed deterministic M0.0 workspaces",
        "Run PostgreSQL integration tests",
        "Check SQLAlchemy metadata against the migrated schema",
        "Downgrade the disposable database to base",
        "Upgrade the disposable database back to head",
        "Recheck schema after the migration cycle",
        "Remove isolated PostgreSQL and its volume",
    ]
    assert all(TEST_PASSWORD not in argument for _, command, _ in calls for argument in command)
    assert calls[-1][1][-3:] == ("down", "--volumes", "--remove-orphans")
    assert calls[0][2]["POSTGRES_PASSWORD"] == TEST_PASSWORD


def test_verification_cleans_up_and_preserves_failure_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_deterministic_runtime(monkeypatch)
    labels: list[str] = []

    def fail_during_tests(
        label: str,
        command: Sequence[str],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        del working_directory, environment, check
        labels.append(label)
        if label == "Run PostgreSQL integration tests":
            raise subprocess.CalledProcessError(7, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verify_postgres, "run_command", fail_during_tests)

    result = verify_postgres.verify_postgres()

    assert result == 7
    assert labels[-1] == "Remove isolated PostgreSQL and its volume"
    assert "Check SQLAlchemy metadata against the migrated schema" not in labels
