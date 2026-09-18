"""Dependency readiness probes with safe operational failure signals."""

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from investment_research.infrastructure.logging import application_logger

REQUIRED_DATABASE_REVISIONS = frozenset({"0006_m1_indexing_lifecycle"})

_logger = application_logger("health")


class ReadinessProbe(Protocol):
    """Report whether one process dependency is ready for application traffic."""

    def is_ready(self) -> bool:
        """Return readiness without raising dependency details across the boundary."""


@dataclass(frozen=True, slots=True)
class CompositeReadinessProbe:
    """Evaluate every required dependency without disclosing which one failed."""

    probes: tuple[ReadinessProbe, ...]

    def __post_init__(self) -> None:
        if not self.probes:
            raise ValueError("at least one readiness probe is required")

    def is_ready(self) -> bool:
        """Return true only when every configured dependency is ready."""

        ready = True
        for probe in self.probes:
            try:
                if not probe.is_ready():
                    ready = False
            except Exception as error:
                _logger.warning(
                    "health.readiness.probe_failed",
                    extra={"exception_type": type(error).__name__},
                )
                ready = False
        return ready


@dataclass(frozen=True, slots=True)
class PostgreSQLReadinessProbe:
    """Check PostgreSQL connectivity and exact Alembic schema compatibility."""

    engine: Engine
    required_revisions: frozenset[str] = REQUIRED_DATABASE_REVISIONS

    def is_ready(self) -> bool:
        """Return false for an unavailable, unmigrated, or stale canonical database."""

        try:
            with self.engine.connect() as connection:
                current_revisions = frozenset(
                    connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
                )
        except SQLAlchemyError as error:
            _logger.warning(
                "health.postgresql.unavailable",
                extra={"exception_type": type(error).__name__},
            )
            return False

        if current_revisions != self.required_revisions:
            _logger.warning("health.postgresql.migration_mismatch")
            return False
        return True
