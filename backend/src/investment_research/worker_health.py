"""Bounded Docker health check for the local Celery worker."""

from collections.abc import Mapping
from socket import gethostname
from typing import Protocol, cast

from celery import Celery  # type: ignore[import-untyped]
from celery.exceptions import CeleryError  # type: ignore[import-untyped]
from kombu.exceptions import KombuError  # type: ignore[import-untyped]
from redis.exceptions import RedisError

WORKER_HEALTH_TIMEOUT_SECONDS = 5.0


class WorkerInspector(Protocol):
    """Small Celery inspection surface used by the health command."""

    def ping(self) -> Mapping[str, Mapping[str, str]] | None:
        """Return worker ping responses keyed by node name."""


class WorkerControl(Protocol):
    """Small Celery control surface used to construct an inspector."""

    def inspect(
        self,
        *,
        destination: list[str],
        timeout: float,
    ) -> WorkerInspector:
        """Return an inspector scoped to the current worker node."""


def worker_node_name(hostname: str | None = None) -> str:
    """Return Celery's default node name for one container hostname."""

    return f"celery@{hostname or gethostname()}"


def worker_is_healthy(
    application: Celery,
    *,
    hostname: str | None = None,
) -> bool:
    """Return whether this exact worker answers a bounded control ping."""

    node_name = worker_node_name(hostname)
    control = cast(WorkerControl, application.control)
    try:
        responses = control.inspect(
            destination=[node_name],
            timeout=WORKER_HEALTH_TIMEOUT_SECONDS,
        ).ping()
    except CeleryError, KombuError, RedisError, OSError:
        return False
    return responses is not None and responses.get(node_name) == {"ok": "pong"}


def main() -> int:
    """Return a process exit status suitable for Docker HEALTHCHECK."""

    from investment_research.worker import app

    return 0 if worker_is_healthy(app) else 1


if __name__ == "__main__":
    raise SystemExit(main())
