"""Unit tests for deterministic backend verification commands."""

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts import verify_backend, verify_paths


def test_backend_verification_runs_each_check_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    def record_command(
        label: str,
        command: Sequence[str],
        *,
        working_directory: Path = verify_backend.BACKEND_ROOT,
    ) -> subprocess.CompletedProcess[bytes]:
        del working_directory
        calls.append((label, tuple(command)))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verify_backend, "run_command", record_command)

    assert verify_backend.verify_backend() == 0
    assert [label for label, _ in calls] == [
        "Check Python formatting",
        "Lint Python",
        "Type-check Python",
        "Run backend unit tests",
    ]
    assert calls[0][1] == (sys.executable, "-m", "ruff", "format", "--check", ".")
    assert calls[-1][1] == (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/unit",
    )


def test_backend_verification_stops_after_first_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels: list[str] = []

    def fail_lint(
        label: str,
        command: Sequence[str],
        *,
        working_directory: Path = verify_backend.BACKEND_ROOT,
    ) -> subprocess.CompletedProcess[bytes]:
        del working_directory
        labels.append(label)
        return subprocess.CompletedProcess(command, 9 if label == "Lint Python" else 0)

    monkeypatch.setattr(verify_backend, "run_command", fail_lint)

    assert verify_backend.verify_backend() == 9
    assert labels == ["Check Python formatting", "Lint Python"]


def test_path_verification_checks_selected_files_and_only_runs_test_modules(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend_root = tmp_path / "backend"
    source = backend_root / "src" / "example.py"
    test = backend_root / "tests" / "unit" / "test_example.py"
    helper = backend_root / "tests" / "integration" / "factories.py"
    for path in (source, test, helper):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n", encoding="utf-8")

    calls: list[tuple[str, tuple[str, ...]]] = []

    def record_command(
        label: str,
        command: Sequence[str],
        *,
        working_directory: Path = backend_root,
    ) -> subprocess.CompletedProcess[bytes]:
        del working_directory
        calls.append((label, tuple(command)))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verify_paths, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(verify_paths, "run_command", record_command)

    assert (
        verify_paths.verify_paths(
            [
                "src/example.py",
                "tests/unit/test_example.py",
                "tests/integration/factories.py",
                "src/example.py",
            ]
        )
        == 0
    )
    selected_paths = (
        "src/example.py",
        "tests/unit/test_example.py",
        "tests/integration/factories.py",
    )
    assert [label for label, _ in calls] == [
        "Check selected Python formatting",
        "Lint selected Python files",
        "Type-check selected Python files",
        "Run selected backend tests",
    ]
    assert calls[0][1][-3:] == selected_paths
    assert calls[1][1][-3:] == selected_paths
    assert calls[2][1][-3:] == (
        "src",
        "tests/unit/test_example.py",
        "tests/integration/factories.py",
    )
    assert calls[3][1][-1] == "tests/unit/test_example.py"
    assert "tests/integration/factories.py" not in calls[3][1]


@pytest.mark.parametrize(
    "raw_path",
    [
        "missing.py",
        "README.md",
        "../outside.py",
    ],
)
def test_path_verification_rejects_invalid_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw_path: str,
) -> None:
    backend_root = tmp_path / "backend"
    backend_root.mkdir()
    (backend_root / "README.md").write_text("not Python\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text("\n", encoding="utf-8")
    monkeypatch.setattr(verify_paths, "BACKEND_ROOT", backend_root)

    assert verify_paths.verify_paths([raw_path]) == 2
