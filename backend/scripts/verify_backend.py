"""Run the complete deterministic backend verification without PostgreSQL."""

import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFICATION_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Check Python formatting",
        (sys.executable, "-m", "ruff", "format", "--check", "."),
    ),
    ("Lint Python", (sys.executable, "-m", "ruff", "check", ".")),
    ("Type-check Python", (sys.executable, "-m", "mypy")),
    ("Run backend unit tests", (sys.executable, "-m", "pytest", "-q", "tests/unit")),
)


def run_command(
    label: str,
    command: Sequence[str],
    *,
    working_directory: Path = BACKEND_ROOT,
) -> subprocess.CompletedProcess[bytes]:
    """Run and display one command without invoking a shell."""

    print(f"\n==> {label}", flush=True)
    print(f"$ {shlex.join(command)}", flush=True)
    return subprocess.run(command, cwd=working_directory, check=False)


def verify_backend() -> int:
    """Run deterministic static checks and unit tests, stopping on first failure."""

    for label, command in VERIFICATION_COMMANDS:
        result = run_command(label, command)
        if result.returncode != 0:
            return result.returncode
    return 0


def main() -> int:
    """Run the backend verification command."""

    return verify_backend()


if __name__ == "__main__":
    raise SystemExit(main())
