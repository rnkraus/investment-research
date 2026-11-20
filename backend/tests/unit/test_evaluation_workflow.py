"""Exercise short evaluation commands without paid calls or real Docker execution."""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts import evaluation_workflow as workflow
from tests.unit.test_evaluate_portfolio import saved as saved
from tests.unit.test_evaluation_profiles import prepared as prepared
from tests.unit.test_evaluation_report import bundle as bundle


def call(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> int:
    monkeypatch.setattr(sys, "argv", ["workflow", *arguments])
    return workflow.main()


def test_short_preview_and_run_without_execution_never_launch_docker(
    bundle: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, profile = bundle
    process = Mock(side_effect=AssertionError("Docker must not run"))
    monkeypatch.setattr(subprocess, "run", process)
    assert call(monkeypatch, "preview", "--profile", profile.name) == 0
    assert call(monkeypatch, "run", "--profile", profile.name) == 0
    assert call(monkeypatch, "run", "--profile", profile.name, "--execute") == 1
    process.assert_not_called()


def test_short_run_preserves_verified_docker_arguments_and_builds_an_offline_report(
    bundle: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, profile = bundle
    monkeypatch.setattr(shutil, "which", lambda _: "docker")

    def execute(arguments: list[str], **kwargs: Any) -> Mock:
        assert kwargs == {"cwd": root, "check": False}
        assert arguments[:3] == ["docker", "compose", "--env-file"]
        assert arguments[3] == ".env"
        assert "--no-deps" in arguments and "--confirm-public-synthetic" in arguments
        volumes = [arguments[index + 1] for index, arg in enumerate(arguments) if arg == "--volume"]
        assert len(volumes) == 4 and sum(value.endswith(":ro") for value in volumes) == 3
        output = arguments[arguments.index("--output") + 1]
        shutil.copytree(root / "outputs/run", root / output)
        return Mock(returncode=0)

    process = Mock(side_effect=execute)
    monkeypatch.setattr(subprocess, "run", process)
    assert (
        call(
            monkeypatch,
            "run",
            "--profile",
            profile.name,
            "--execute",
            "--confirm-public-synthetic",
            "--output",
            "outputs/new-run",
        )
        == 0
    )
    process.assert_called_once()
    assert len(list((root / "outputs/evaluation-reports").glob("*.html"))) == 1


def test_short_report_and_compare_commands_create_reports_without_docker(
    bundle: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = bundle
    process = Mock(side_effect=AssertionError("offline command must not run Docker"))
    monkeypatch.setattr(subprocess, "run", process)
    assert call(monkeypatch, "report", "outputs/run", "--output", "outputs/report.html") == 0
    assert (
        call(
            monkeypatch, "compare", "outputs/run", "outputs/run", "--output", "outputs/compare.html"
        )
        == 0
    )
    assert (root / "outputs/report.html").is_file() and (root / "outputs/compare.html").is_file()
    process.assert_not_called()
