"""Guard the disposable fixture entry point and its bounded real-clock expiry wait."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from investment_research.settings_models import ApiSettings
from scripts import queue_expired_execution_probe as probe


def test_unapproved_probe_does_not_load_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(probe.VERIFICATION_FLAG, raising=False)
    load = Mock()
    monkeypatch.setattr(probe, "load_api_settings", load)
    assert probe.main() == 2
    load.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": "development"},
        {"runtime_location": "host"},
        {"processing_profile": "m1"},
    ],
)
def test_nonisolated_profile_cannot_build_resources(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, str]
) -> None:
    monkeypatch.setenv(probe.VERIFICATION_FLAG, "1")
    values = {
        "environment": "test",
        "runtime_location": "container",
        "processing_profile": "m0-regression",
    }
    settings = Mock(spec=ApiSettings, **(values | changes))
    load = Mock(return_value=settings)
    build = Mock()
    monkeypatch.setattr(probe, "load_api_settings", load)
    monkeypatch.setattr(probe, "build_api_container", build)
    assert probe.main() == 2
    load.assert_called_once_with(env_file=None)
    build.assert_not_called()


@pytest.mark.parametrize("fail", [False, True])
def test_probe_resources_close_on_success_and_failure(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    monkeypatch.setenv(probe.VERIFICATION_FLAG, "1")
    settings = Mock(
        spec=ApiSettings,
        environment="test",
        runtime_location="container",
        processing_profile="m0-regression",
    )
    container = Mock()
    verify = Mock(side_effect=AssertionError("synthetic failure") if fail else None)
    monkeypatch.setattr(probe, "load_api_settings", Mock(return_value=settings))
    monkeypatch.setattr(probe, "build_api_container", Mock(return_value=container))
    monkeypatch.setattr(probe, "verify", verify)
    if fail:
        with pytest.raises(AssertionError, match="synthetic failure"):
            probe.main()
    else:
        assert probe.main() == 0
    verify.assert_called_once_with(container)
    container.close.assert_called_once_with()


def test_expiry_wait_uses_real_elapsed_time_without_mutating_state() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    elapsed = 0.0

    def advance(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    probe.wait_for_expiry(
        now + timedelta(seconds=1),
        clock=lambda: now + timedelta(seconds=elapsed),
        monotonic_clock=lambda: elapsed,
        sleep=advance,
    )
    assert elapsed == 1


def test_expiry_wait_cannot_hang_if_utc_clock_stops() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    elapsed = 0.0

    def advance(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    with pytest.raises(TimeoutError, match="before timeout"):
        probe.wait_for_expiry(
            now + timedelta(seconds=1),
            clock=lambda: now,
            monotonic_clock=lambda: elapsed,
            sleep=advance,
        )
    assert elapsed == probe.WAIT_SECONDS
