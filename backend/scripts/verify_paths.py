"""Run deterministic Python checks for explicitly selected backend files."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.verify_backend import BACKEND_ROOT, run_command


def normalize_paths(raw_paths: Sequence[str]) -> tuple[str, ...]:
    """Return unique backend-relative Python file paths after strict validation."""

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        candidate = Path(raw_path)
        resolved = (
            candidate.resolve() if candidate.is_absolute() else (BACKEND_ROOT / candidate).resolve()
        )
        try:
            relative = resolved.relative_to(BACKEND_ROOT)
        except ValueError as error:
            raise ValueError(f"path is outside the backend: {raw_path}") from error
        if not resolved.is_file():
            raise ValueError(f"path is not an existing file: {raw_path}")
        if resolved.suffix != ".py":
            raise ValueError(f"path is not a Python file: {raw_path}")

        rendered = relative.as_posix()
        if rendered not in seen:
            normalized.append(rendered)
            seen.add(rendered)
    return tuple(normalized)


def select_pytest_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """Return selected paths that Pytest should collect as test modules."""

    return tuple(
        path
        for path in paths
        if Path(path).is_relative_to("tests") and Path(path).name.startswith("test_")
    )


def verify_paths(raw_paths: Sequence[str]) -> int:
    """Check selected Python files and run any selected test modules."""

    try:
        paths = normalize_paths(raw_paths)
    except ValueError as error:
        print(f"Path error: {error}", file=sys.stderr)
        return 2

    commands: list[tuple[str, tuple[str, ...]]] = [
        (
            "Check selected Python formatting",
            (sys.executable, "-m", "ruff", "format", "--check", *paths),
        ),
        ("Lint selected Python files", (sys.executable, "-m", "ruff", "check", *paths)),
        ("Type-check selected Python files", (sys.executable, "-m", "mypy", *paths)),
    ]
    pytest_paths = select_pytest_paths(paths)
    if pytest_paths:
        commands.append(
            (
                "Run selected backend tests",
                (sys.executable, "-m", "pytest", "-q", *pytest_paths),
            )
        )

    for label, command in commands:
        result = run_command(label, command)
        if result.returncode != 0:
            return result.returncode
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for explicit backend file paths."""

    parser = argparse.ArgumentParser(
        description="Run Ruff, Mypy, and applicable Pytest checks for selected backend files."
    )
    parser.add_argument("paths", nargs="+", help="Python files relative to the backend directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse file paths and run their deterministic checks."""

    arguments = build_parser().parse_args(argv)
    return verify_paths(arguments.paths)


if __name__ == "__main__":
    raise SystemExit(main())
