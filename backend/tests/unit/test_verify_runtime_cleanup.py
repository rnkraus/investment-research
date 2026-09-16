"""Unit tests for the M0 runtime cleanup verification."""

import subprocess
from collections.abc import Sequence

import pytest

from scripts import verify_runtime_cleanup


def test_runtime_cleanup_verification_accepts_an_empty_scoped_inventory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands: list[tuple[str, ...]] = []

    def empty_inventory(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="unrelated-resource\n", stderr="")

    monkeypatch.setattr(verify_runtime_cleanup, "find_docker_executable", lambda: "/usr/bin/docker")
    monkeypatch.setattr(verify_runtime_cleanup, "run_command", empty_inventory)

    assert verify_runtime_cleanup.verify_runtime_cleanup() == 0
    assert len(commands) == 4
    assert all(command[0] == "/usr/bin/docker" for command in commands)
    assert "No isolated M0 verification resources remain." in capsys.readouterr().out


def test_runtime_cleanup_verification_reports_only_scoped_leaks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outputs = iter(
        (
            "investment-research-m0-check-deadbeef-api-1\nother-container\n",
            "investment-research-parser-check-deadbeef_parser\nother-network\n",
            "investment-research-queue-check-deadbeef_postgres-data\nother-volume\n",
            "investment-research-m0-acceptance:deadbeef\nother-image:latest\n",
        )
    )

    def leaked_inventory(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=next(outputs), stderr="")

    monkeypatch.setattr(verify_runtime_cleanup, "find_docker_executable", lambda: "/usr/bin/docker")
    monkeypatch.setattr(verify_runtime_cleanup, "run_command", leaked_inventory)

    assert verify_runtime_cleanup.verify_runtime_cleanup() == 1
    captured = capsys.readouterr()
    assert "other-" not in captured.err
    assert "container: investment-research-m0-check-deadbeef-api-1" in captured.err
    assert "network: investment-research-parser-check-deadbeef_parser" in captured.err
    assert "volume: investment-research-queue-check-deadbeef_postgres-data" in captured.err
    assert "image: investment-research-m0-acceptance:deadbeef" in captured.err


def test_runtime_cleanup_verification_requires_docker(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(verify_runtime_cleanup, "find_docker_executable", lambda: None)

    assert verify_runtime_cleanup.verify_runtime_cleanup() == 127
    assert "Docker is required" in capsys.readouterr().err


def test_runtime_cleanup_verification_fails_when_inventory_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failed_inventory(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 9, stdout="", stderr="private Docker failure")

    monkeypatch.setattr(verify_runtime_cleanup, "find_docker_executable", lambda: "/usr/bin/docker")
    monkeypatch.setattr(verify_runtime_cleanup, "run_command", failed_inventory)

    assert verify_runtime_cleanup.verify_runtime_cleanup() == 9
    captured = capsys.readouterr()
    assert "Could not inspect Docker container resources." in captured.err
    assert "private Docker failure" not in captured.err
