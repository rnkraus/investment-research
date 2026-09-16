"""Verify that M0 runtime checks left no scoped Docker resources behind."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence

PROJECT_PREFIXES = (
    "investment-research-parser-check-",
    "investment-research-queue-check-",
    "investment-research-m0-check-",
)
ACCEPTANCE_IMAGE_PREFIXES = ("investment-research-m0-acceptance:",)
RESOURCE_COMMANDS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("container", ("container", "ls", "--all", "--format", "{{.Names}}"), PROJECT_PREFIXES),
    ("network", ("network", "ls", "--format", "{{.Name}}"), PROJECT_PREFIXES),
    ("volume", ("volume", "ls", "--format", "{{.Name}}"), PROJECT_PREFIXES),
    (
        "image",
        ("image", "ls", "--format", "{{.Repository}}:{{.Tag}}"),
        ACCEPTANCE_IMAGE_PREFIXES,
    ),
)


def find_docker_executable() -> str | None:
    """Return the Docker CLI path when it is available."""

    return shutil.which("docker")


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one read-only Docker inventory command without a shell."""

    return subprocess.run(command, check=False, capture_output=True, text=True)


def matching_names(output: str, prefixes: Sequence[str]) -> tuple[str, ...]:
    """Return sorted resource names owned by an M0 verification prefix."""

    return tuple(
        sorted(
            name
            for line in output.splitlines()
            if (name := line.strip()) and name.startswith(tuple(prefixes))
        )
    )


def verify_runtime_cleanup() -> int:
    """Fail when a completed M0 verification left a scoped Docker resource behind."""

    docker = find_docker_executable()
    if docker is None:
        print("Docker is required for runtime cleanup verification.", file=sys.stderr)
        return 127

    leaked_resources: list[tuple[str, str]] = []
    for resource_type, arguments, prefixes in RESOURCE_COMMANDS:
        result = run_command((docker, *arguments))
        if result.returncode != 0:
            print(
                f"Could not inspect Docker {resource_type} resources.",
                file=sys.stderr,
            )
            return result.returncode or 1
        leaked_resources.extend(
            (resource_type, name) for name in matching_names(result.stdout, prefixes)
        )

    if leaked_resources:
        print("M0 verification resources remain after cleanup:", file=sys.stderr)
        for resource_type, name in leaked_resources:
            print(f"- {resource_type}: {name}", file=sys.stderr)
        return 1

    print("No isolated M0 verification resources remain.", flush=True)
    return 0


def main() -> int:
    """Run the M0 runtime cleanup verification."""

    return verify_runtime_cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
