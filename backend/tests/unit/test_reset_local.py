"""Unit tests for the fixed-target local reset workflow."""

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts import reset_local


def configure_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    """Create and configure only the repository markers validated by the script."""

    repository_root = tmp_path / "investment-research"
    backend_root = repository_root / "backend"
    backend_root.mkdir(parents=True)
    (repository_root / ".git").mkdir()
    compose_file = repository_root / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    (backend_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (backend_root / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    monkeypatch.setattr(reset_local, "REPOSITORY_ROOT", repository_root)
    monkeypatch.setattr(reset_local, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(reset_local, "COMPOSE_FILE", compose_file)
    monkeypatch.setattr(reset_local, "ENV_FILE", repository_root / ".env")
    return repository_root, backend_root


def test_reset_rejects_wrong_working_directory_before_docker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository_root, _ = configure_repository(monkeypatch, tmp_path)
    monkeypatch.setattr(
        reset_local,
        "find_docker_executable",
        lambda: pytest.fail("Docker discovery must not run"),
    )

    assert (
        reset_local.reset_local(
            confirmation=reset_local.CONFIRMATION_PHRASE,
            working_directory=repository_root,
        )
        == 2
    )


def test_reset_requires_exact_confirmation_before_docker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, backend_root = configure_repository(monkeypatch, tmp_path)
    monkeypatch.setattr(
        reset_local,
        "find_docker_executable",
        lambda: pytest.fail("Docker discovery must not run"),
    )

    assert reset_local.reset_local(confirmation="yes", working_directory=backend_root) == 2


def test_reset_reports_unavailable_docker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, backend_root = configure_repository(monkeypatch, tmp_path)
    monkeypatch.setattr(reset_local, "find_docker_executable", lambda: None)

    assert (
        reset_local.reset_local(
            confirmation=reset_local.CONFIRMATION_PHRASE,
            working_directory=backend_root,
        )
        == 127
    )


def test_reset_targets_only_fixed_project_then_restarts_and_seeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, backend_root = configure_repository(monkeypatch, tmp_path)
    calls: list[tuple[str, ...]] = []

    def record_command(
        label: str,
        command: Sequence[str],
    ) -> subprocess.CompletedProcess[bytes]:
        del label
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(reset_local, "find_docker_executable", lambda: "/usr/bin/docker")
    monkeypatch.setattr(reset_local, "run_command", record_command)

    assert (
        reset_local.reset_local(
            confirmation=reset_local.CONFIRMATION_PHRASE,
            working_directory=backend_root,
        )
        == 0
    )
    assert len(calls) == 3
    for command in calls:
        assert command[:4] == (
            "docker",
            "compose",
            "--project-name",
            reset_local.LOCAL_PROJECT_NAME,
        )
    assert calls[0][-3:] == (
        "down",
        "--volumes",
        "--remove-orphans",
    )
    assert calls[1][-4:] == ("up", "--build", "--detach", "--wait")
    assert calls[2][-7:] == (
        "--profile",
        "tools",
        "run",
        "--build",
        "--rm",
        "--no-deps",
        "seed",
    )


def test_reset_stops_before_seed_when_restart_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, backend_root = configure_repository(monkeypatch, tmp_path)
    labels: list[str] = []

    def fail_restart(
        label: str,
        command: Sequence[str],
    ) -> subprocess.CompletedProcess[bytes]:
        labels.append(label)
        if label == "Rebuild and start the complete local runtime":
            raise subprocess.CalledProcessError(9, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(reset_local, "find_docker_executable", lambda: "/usr/bin/docker")
    monkeypatch.setattr(reset_local, "run_command", fail_restart)

    assert (
        reset_local.reset_local(
            confirmation=reset_local.CONFIRMATION_PHRASE,
            working_directory=backend_root,
        )
        == 9
    )
    assert labels == [
        "Remove only the local runtime and its named volumes",
        "Rebuild and start the complete local runtime",
    ]
