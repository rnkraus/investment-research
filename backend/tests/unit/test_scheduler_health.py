"""Scheduler liveness must not depend on credentials, broker availability, or worker bootstrap."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from investment_research import scheduler_health as health


def proc_fixture(root: Path, *, children: str = "7", command: bytes | None = None) -> None:
    child_list = root / "1/task/1/children"
    child_list.parent.mkdir(parents=True)
    child_list.write_text(children, encoding="ascii")
    if command is not None:
        process = root / "7"
        process.mkdir()
        (process / "cmdline").write_bytes(command)


def test_exact_init_supervised_scheduler_is_live_without_runtime_configuration(
    tmp_path: Path,
) -> None:
    proc_fixture(tmp_path, children="7 8", command=b"\0".join(health.SCHEDULER_COMMAND) + b"\0")
    assert health.scheduler_is_running(tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        b"",
        b"celery\0worker\0",
        b"/app/.venv/bin/python\0-m\0investment_research.scheduler_health\0",
        b"/app/.venv/bin/python\0-m\0investment_research.scheduler.evil\0",
        b"/app/.venv/bin/python\0-m\0investment_research.scheduler\0--help\0",
    ],
)
def test_other_processes_and_exited_scheduler_do_not_pass(tmp_path: Path, command: bytes) -> None:
    proc_fixture(tmp_path, command=command)
    assert not health.scheduler_is_running(tmp_path)


@pytest.mark.parametrize("children", ["", "7", "../7", "7/other", "-7"])
def test_absent_exited_or_invalid_children_fail_closed(tmp_path: Path, children: str) -> None:
    proc_fixture(tmp_path, children=children)
    assert not health.scheduler_is_running(tmp_path)


def test_unreadable_procfs_fails_without_printing_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=PermissionError("must-not-appear")))
    assert not health.scheduler_is_running()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("running", [False, True])
def test_command_reports_liveness_without_starting_scheduler(
    monkeypatch: pytest.MonkeyPatch, running: bool
) -> None:
    monkeypatch.setattr(health, "scheduler_is_running", Mock(return_value=running))
    assert health.main() == (0 if running else 1)
