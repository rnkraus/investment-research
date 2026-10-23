"""Broker-independent liveness for the dedicated, init-supervised scheduler process."""

from pathlib import Path

SCHEDULER_COMMAND = (
    b"/app/.venv/bin/python",
    b"-m",
    b"investment_research.scheduler",
)


def scheduler_is_running(proc_root: Path = Path("/proc")) -> bool:
    """Inspect only PID 1's direct children; never load settings, secrets, or worker resources."""

    try:
        children = (proc_root / "1/task/1/children").read_text(encoding="ascii").split()
        for child in children:
            if not child.isascii() or not child.isdecimal():
                return False
            try:
                command = (proc_root / child / "cmdline").read_bytes()
            except FileNotFoundError:
                continue  # A process can exit between the two procfs reads.
            if tuple(command.rstrip(b"\0").split(b"\0")) == SCHEDULER_COMMAND:
                return True
    except OSError, UnicodeError:
        return False
    return False


def main() -> int:
    """Report process liveness only, not successful publication or completed processing."""

    return 0 if scheduler_is_running() else 1


if __name__ == "__main__":
    raise SystemExit(main())
