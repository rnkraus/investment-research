"""Short repository commands for free previews, opt-in Docker runs and saved HTML reports."""

import argparse
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from scripts import evaluation_profiles
from scripts.evaluation_profiles import resolve_profile
from scripts.evaluation_report import render_comparison, render_run, write_report
from scripts.evaluation_run_artifacts import load_run, run_directory
from scripts.run_portfolio_evaluation import preview

DEFAULT_PROFILE_NAME = "full-retrieval.example.toml"


def profile_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    root = evaluation_profiles.REPOSITORY_ROOT
    return root / path if path.parts and path.parts[0] == "eval" else root / "eval/profiles" / path


def new_reference(directory: str, name: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"outputs/{directory}/{name}-{stamp}-{uuid4().hex[:8]}"


def docker_command(profile: Path, output: str, cases: tuple[str, ...]) -> list[str]:
    """Preserve the previously verified Compose topology, mounts and unprivileged user."""

    root = evaluation_profiles.REPOSITORY_ROOT
    relative = profile.resolve().relative_to((root / "eval/profiles").resolve())
    command = [
        "docker",
        "compose",
        "--env-file",
        ".env",
        "-f",
        "compose.yaml",
        "-f",
        "compose.m1-live.yaml",
        "run",
        "--rm",
        "--no-deps",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--entrypoint",
        "/app/.venv/bin/python",
    ]
    for source, target, writable in (
        ("backend/scripts", "backend/scripts", False),
        ("eval", "eval", False),
        ("config", "config", False),
        ("outputs", "outputs", True),
    ):
        command.extend(
            ("--volume", f"{root / source}:/evaluation/{target}" + ("" if writable else ":ro"))
        )
    command.extend(
        (
            "--workdir",
            "/evaluation/backend",
            "api",
            "-m",
            "scripts.run_portfolio_evaluation",
            "--profile",
            f"../eval/profiles/{relative.as_posix()}",
            "--execute",
            "--confirm-public-synthetic",
            "--output",
            output,
        )
    )
    for case in cases:
        command.extend(("--case", case))
    return command


def save_run_report(reference: str, output: str | None = None) -> Path:
    run = load_run(reference)
    destination = (
        output or new_reference("evaluation-reports", run.definition.profile.name) + ".html"
    )
    return write_report(destination, render_run(run))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "run"):
        command = commands.add_parser(name)
        command.add_argument("--profile", default=DEFAULT_PROFILE_NAME)
        command.add_argument("--case", action="append", default=[])
        if name == "run":
            command.add_argument("--execute", action="store_true")
            command.add_argument("--confirm-public-synthetic", action="store_true")
            command.add_argument("--output", help="New run directory beneath outputs/")
    report = commands.add_parser("report")
    report.add_argument("run", help="Saved run directory beneath outputs/")
    report.add_argument("--output", help="New .html file beneath outputs/")
    compare = commands.add_parser("compare")
    compare.add_argument("baseline", help="Earlier saved run directory")
    compare.add_argument("candidate", help="Later saved run directory")
    compare.add_argument("--output", help="New .html file beneath outputs/")
    args = parser.parse_args()
    try:
        if args.command == "report":
            path = save_run_report(args.run, args.output)
            print(f"Report written: {path}")
            return 0
        if args.command == "compare":
            baseline, candidate = load_run(args.baseline), load_run(args.candidate)
            rendered = render_comparison(baseline, candidate)
            destination = args.output or new_reference("evaluation-reports", "comparison") + ".html"
            print(f"Comparison written: {write_report(destination, rendered)}")
            return 0
        path = profile_path(args.profile)
        resolved = resolve_profile(path, case_ids=tuple(args.case))
        plan = preview(resolved)
        print(json.dumps(plan, indent=2), flush=True)
        if args.command == "preview" or not args.execute:
            print("Preview only. No provider calls were made.")
            return 0
        if not args.confirm_public_synthetic:
            raise ValueError("public/synthetic confirmation required")
        output = args.output or new_reference("portfolio-live", resolved.profile.name)
        directory = run_directory(output)
        if directory.exists() or directory.name == "outputs":
            raise FileExistsError("choose a new run directory")
        if shutil.which("docker") is None:
            raise ValueError("Docker is required for live execution")
        root = evaluation_profiles.REPOSITORY_ROOT
        (root / "outputs").mkdir(exist_ok=True)
        print(f"Planned run directory: {root / output}", flush=True)
        completed = subprocess.run(
            docker_command(path, output, tuple(args.case)),
            cwd=root,
            check=False,
        )
        if completed.returncode:
            print("Evaluation stopped. Retain the run's journal and manifest for diagnosis.")
            return completed.returncode
        print(f"Report written: {save_run_report(output)}")
        return 0
    except (OSError, ValueError, TypeError) as error:
        print(
            f"Evaluation workflow rejected ({type(error).__name__}). "
            "Check the profile, saved artifact consistency and unused output path."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
