"""Catch live-runner fixture initialization failures without PostgreSQL or providers."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tests.integration.test_m1_5_live_runner_postgresql import ImmediateIndex


def test_runner_index_initializes_frozen_fields_and_owns_a_fresh_workspace(tmp_path: Path) -> None:
    first = ImmediateIndex(tmp_path / "first.sqlite")
    second = ImmediateIndex(tmp_path / "second.sqlite")
    try:
        assert first.database_path.is_file()
        assert second.database_path.is_file()
        assert first.context.workspace_id != second.context.workspace_id
        for attribute in ("context", "database_path"):
            with pytest.raises(FrozenInstanceError):
                setattr(first, attribute, getattr(second, attribute))
    finally:
        first.close()
        second.close()
