"""Reset only the named local Compose runtime and recreate its synthetic seed."""

import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
ENV_FILE = REPOSITORY_ROOT / ".env"
LOCAL_PROJECT_NAME = "investment-research-m0-0"
CONFIRMATION_PHRASE = f"reset {LOCAL_PROJECT_NAME}"


class LocalResetError(RuntimeError):
    """Report an unsafe or invalid local-reset invocation."""


def find_docker_executable() -> str | None:
    """Return the Docker executable path when it is available."""

    return shutil.which("docker")


def validate_local_repository(working_directory: Path) -> None:
    """Require the exact repository and backend working directory."""

    if working_directory.resolve() != BACKEND_ROOT.resolve():
        raise LocalResetError("run the local reset from the repository backend directory")

    required_markers = (
        REPOSITORY_ROOT / ".git",
        COMPOSE_FILE,
        BACKEND_ROOT / "pyproject.toml",
        BACKEND_ROOT / "uv.lock",
    )
    if any(not marker.exists() for marker in required_markers):
        raise LocalResetError("the expected investment-research repository markers are unavailable")


def build_compose_command() -> list[str]:
    """Return the fixed local Compose command without reading environment values."""

    return [
        "docker",
        "compose",
        "--project-name",
        LOCAL_PROJECT_NAME,
        "--file",
        str(COMPOSE_FILE),
        "--env-file",
        str(ENV_FILE),
    ]


def run_command(label: str, command: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    """Run one visible fixed-target command without invoking a shell."""

    print(f"\n==> {label}", flush=True)
    print(f"$ {shlex.join(command)}", flush=True)
    return subprocess.run(command, cwd=REPOSITORY_ROOT, check=True)


def reset_local(*, confirmation: str, working_directory: Path | None = None) -> int:
    """Reset, restart, and seed the single fixed local development project."""

    try:
        validate_local_repository(working_directory or Path.cwd())
    except LocalResetError as error:
        print(str(error), file=sys.stderr)
        return 2

    if confirmation != CONFIRMATION_PHRASE:
        print("Local reset cancelled; the confirmation phrase did not match.", file=sys.stderr)
        return 2
    if find_docker_executable() is None:
        print("Docker is required for the local reset.", file=sys.stderr)
        return 127

    compose_command = build_compose_command()
    try:
        run_command(
            "Remove only the local runtime and its named volumes",
            [*compose_command, "down", "--volumes", "--remove-orphans"],
        )
        run_command(
            "Rebuild and start the complete local runtime",
            [*compose_command, "up", "--build", "--detach", "--wait"],
        )
        run_command(
            "Seed deterministic synthetic workspaces",
            [
                *compose_command,
                "--profile",
                "tools",
                "run",
                "--build",
                "--rm",
                "--no-deps",
                "seed",
            ],
        )
    except subprocess.CalledProcessError as error:
        return error.returncode or 1
    return 0


def main() -> int:
    """Request explicit destructive confirmation and reset the local runtime."""

    print(
        f"This removes only Docker volumes owned by the local {LOCAL_PROJECT_NAME} Compose project."
    )
    print("All local PostgreSQL and SeaweedFS data in those volumes will be permanently deleted.")
    confirmation = input(f"Type '{CONFIRMATION_PHRASE}' to continue: ")
    return reset_local(confirmation=confirmation)


if __name__ == "__main__":
    raise SystemExit(main())
