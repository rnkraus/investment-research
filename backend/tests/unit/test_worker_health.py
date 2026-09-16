"""Unit tests for the bounded Celery worker health command."""

from collections.abc import Mapping
from typing import cast

import pytest
from celery import Celery  # type: ignore[import-untyped]
from celery.exceptions import CeleryError  # type: ignore[import-untyped]
from kombu.exceptions import KombuError  # type: ignore[import-untyped]
from redis.exceptions import RedisError

from investment_research.worker_health import (
    WORKER_HEALTH_TIMEOUT_SECONDS,
    worker_is_healthy,
    worker_node_name,
)


class StubInspector:
    """Return or raise one configured control-ping outcome."""

    def __init__(
        self,
        response: Mapping[str, Mapping[str, str]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls = 0

    def ping(self) -> Mapping[str, Mapping[str, str]] | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


class StubControl:
    """Record the destination and timeout used to create an inspector."""

    def __init__(self, inspector: StubInspector) -> None:
        self.inspector = inspector
        self.calls: list[tuple[list[str], float]] = []

    def inspect(self, *, destination: list[str], timeout: float) -> StubInspector:
        self.calls.append((destination, timeout))
        return self.inspector


class StubApplication:
    """Expose only the Celery control surface required by the health command."""

    def __init__(self, control: StubControl) -> None:
        self.control = control


def make_application(inspector: StubInspector) -> tuple[Celery, StubControl]:
    """Build one typed Celery stand-in without importing worker configuration."""

    control = StubControl(inspector)
    return cast(Celery, StubApplication(control)), control


def test_worker_health_targets_only_the_current_worker() -> None:
    node_name = worker_node_name("worker-container")
    inspector = StubInspector(response={node_name: {"ok": "pong"}})
    application, control = make_application(inspector)

    assert worker_is_healthy(application, hostname="worker-container")
    assert control.calls == [([node_name], WORKER_HEALTH_TIMEOUT_SECONDS)]
    assert inspector.calls == 1


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(None, id="timeout"),
        pytest.param({}, id="empty"),
        pytest.param({"celery@other": {"ok": "pong"}}, id="other-worker"),
        pytest.param({"celery@worker-container": {"ok": "unexpected"}}, id="bad-payload"),
    ],
)
def test_worker_health_rejects_missing_or_unexpected_responses(
    response: Mapping[str, Mapping[str, str]] | None,
) -> None:
    application, _ = make_application(StubInspector(response=response))

    assert not worker_is_healthy(application, hostname="worker-container")


@pytest.mark.parametrize(
    "provider_error",
    [
        pytest.param(CeleryError("synthetic failure"), id="celery"),
        pytest.param(KombuError("synthetic failure"), id="kombu"),
        pytest.param(RedisError("synthetic failure"), id="redis"),
        pytest.param(OSError("synthetic failure"), id="socket"),
    ],
)
def test_worker_health_fails_closed_for_expected_transport_errors(
    provider_error: Exception,
) -> None:
    application, _ = make_application(StubInspector(error=provider_error))

    assert not worker_is_healthy(application, hostname="worker-container")


def test_worker_health_does_not_hide_programming_errors() -> None:
    application, _ = make_application(StubInspector(error=TypeError("programming error")))

    with pytest.raises(TypeError, match="programming error"):
        worker_is_healthy(application, hostname="worker-container")
