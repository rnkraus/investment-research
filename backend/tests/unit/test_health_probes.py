"""Tests for layered dependency readiness without operational disclosure."""

from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from investment_research.infrastructure.health import (
    REQUIRED_DATABASE_REVISIONS,
    CompositeReadinessProbe,
    PostgreSQLReadinessProbe,
)
from investment_research.infrastructure.logging import configure_logging

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SECRET_MARKER = "synthetic-secret-marker"


@dataclass(slots=True)
class StubReadinessProbe:
    """Record one deterministic readiness result for composition tests."""

    result: bool
    calls: int = 0

    def is_ready(self) -> bool:
        self.calls += 1
        return self.result


class FailingReadinessProbe:
    """Raise one private error to verify the public fail-closed boundary."""

    def is_ready(self) -> bool:
        raise RuntimeError(f"private probe failure: {SECRET_MARKER}")


def test_required_database_revisions_match_alembic_heads() -> None:
    configuration = Config(str(BACKEND_ROOT / "alembic.ini"))
    migration_scripts = ScriptDirectory.from_config(configuration)

    assert frozenset(migration_scripts.get_heads()) == REQUIRED_DATABASE_REVISIONS


def test_postgresql_probe_accepts_only_the_required_revision() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": next(iter(REQUIRED_DATABASE_REVISIONS))},
            )

        assert PostgreSQLReadinessProbe(engine).is_ready()

        with engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": "stale_revision"},
            )

        assert not PostgreSQLReadinessProbe(engine).is_ready()
    finally:
        engine.dispose()


def test_postgresql_probe_fails_closed_when_schema_is_unavailable() -> None:
    stream = StringIO()
    configure_logging(process_name="api", level="INFO", stream=stream)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        assert not PostgreSQLReadinessProbe(engine).is_ready()
    finally:
        engine.dispose()

    assert "health.postgresql.unavailable" in stream.getvalue()
    assert "OperationalError" in stream.getvalue()
    assert "SELECT version_num" not in stream.getvalue()


def test_composite_probe_checks_every_dependency_and_contains_exceptions() -> None:
    stream = StringIO()
    configure_logging(process_name="api", level="INFO", stream=stream)
    unavailable = StubReadinessProbe(result=False)
    available = StubReadinessProbe(result=True)
    probe = CompositeReadinessProbe((unavailable, FailingReadinessProbe(), available))

    assert not probe.is_ready()
    assert unavailable.calls == available.calls == 1
    assert "health.readiness.probe_failed" in stream.getvalue()
    assert "RuntimeError" in stream.getvalue()
    assert SECRET_MARKER not in stream.getvalue()


def test_composite_probe_requires_at_least_one_dependency() -> None:
    with pytest.raises(ValueError, match="at least one readiness probe"):
        CompositeReadinessProbe(())
