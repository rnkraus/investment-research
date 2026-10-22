"""The operator command shares repair bounds, keeps scope trusted, and conceals error details."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from investment_research import repair_outbox
from investment_research.infrastructure.queue.outbox import BoundedCeleryOutboxPublisher
from investment_research.modules.delivery.application import DrainStopReason, OutboxDrainResult
from investment_research.modules.delivery.recovery import OutboxRecoveryResult, RecoveryStopReason
from investment_research.modules.delivery.repair import OutboxRepairResult
from investment_research.modules.identity.application import FixedLocalIdentityContextProvider
from investment_research.modules.processing.application import (
    M0_0_PROCESSING_PROFILE,
    M1_PROCESSING_PROFILE,
)
from investment_research.settings import OutboxRepairSettings


def empty_result() -> OutboxRepairResult:
    return OutboxRepairResult(
        unstarted=OutboxRecoveryResult(0, 0, RecoveryStopReason.SNAPSHOT_EXHAUSTED),
        expired=OutboxRecoveryResult(0, 0, RecoveryStopReason.SNAPSHOT_EXHAUSTED),
        delivery=OutboxDrainResult(0, 0, 0, 0, DrainStopReason.EMPTY),
        used_targets=0,
    )


@pytest.fixture
def command_dependencies(monkeypatch: pytest.MonkeyPatch) -> Mock:
    dependencies = Mock()
    dependencies.settings = OutboxRepairSettings(
        database_url=SecretStr("postgresql+psycopg://test:synthetic@127.0.0.1:5432/test"),
        redis_url=SecretStr("redis://:synthetic@127.0.0.1:6379/0"),
    )
    dependencies.load.return_value = dependencies.settings
    dependencies.readiness.return_value.is_ready.return_value = True
    dependencies.service.repair.return_value = empty_result()
    dependencies.compose.return_value = dependencies.service
    monkeypatch.setattr(repair_outbox, "load_outbox_repair_settings", dependencies.load)
    monkeypatch.setattr(repair_outbox, "create_database_engine", dependencies.engine_factory)
    monkeypatch.setattr(repair_outbox, "PostgreSQLReadinessProbe", dependencies.readiness)
    monkeypatch.setattr(repair_outbox, "create_repair_service", dependencies.compose)
    return dependencies


def test_command_executes_one_pass_in_trusted_scope_and_closes_engine(
    command_dependencies: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert repair_outbox.main([]) == 0
    command_dependencies.service.repair.assert_called_once_with(
        FixedLocalIdentityContextProvider().resolve()
    )
    command_dependencies.engine_factory.return_value.dispose.assert_called_once_with()
    output = capsys.readouterr()
    assert output.err == ""
    payload = json.loads(output.out)
    assert payload["status"] == "pass_completed"
    assert payload["used_targets"] == 0
    assert payload["delivery"]["stop_reason"] == "EMPTY"
    assert "synthetic" not in output.out


@pytest.mark.parametrize("option", ["--force", "--workspace-id", "--reset-attempts", "--loop"])
def test_unapproved_controls_are_rejected_before_settings_load(
    command_dependencies: Mock, option: str
) -> None:
    with pytest.raises(SystemExit) as error:
        repair_outbox.main([option])
    assert error.value.code == 2
    command_dependencies.load.assert_not_called()
    command_dependencies.engine_factory.assert_not_called()


def test_help_does_not_load_configuration_or_connect(command_dependencies: Mock) -> None:
    with pytest.raises(SystemExit) as error:
        repair_outbox.main(["--help"])
    assert error.value.code == 0
    command_dependencies.load.assert_not_called()


@pytest.mark.parametrize("missing", ["database_url", "redis_url"])
def test_missing_connection_configuration_fails_before_engine_creation(
    command_dependencies: Mock, missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    command_dependencies.load.return_value = OutboxRepairSettings.model_validate(
        {**command_dependencies.settings.model_dump(), missing: None}
    )
    assert repair_outbox.main([]) == 1
    command_dependencies.engine_factory.assert_not_called()
    command_dependencies.service.repair.assert_not_called()
    assert capsys.readouterr().err == (
        "Outbox repair failed at stage=configuration; details withheld.\n"
    )


def test_incompatible_database_is_not_repaired_and_is_closed(
    command_dependencies: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    command_dependencies.readiness.return_value.is_ready.return_value = False
    assert repair_outbox.main([]) == 1
    command_dependencies.compose.assert_not_called()
    command_dependencies.engine_factory.return_value.dispose.assert_called_once_with()
    assert "stage=database" in capsys.readouterr().err


@pytest.mark.parametrize("stage", ["configuration", "database", "repair", "cleanup"])
def test_error_output_is_content_free_and_resources_are_closed(
    command_dependencies: Mock, stage: str, capsys: pytest.CaptureFixture[str]
) -> None:
    failure = RuntimeError("must-not-appear secret or document text")
    match stage:
        case "configuration":
            command_dependencies.load.side_effect = failure
        case "database":
            command_dependencies.readiness.return_value.is_ready.side_effect = failure
        case "repair":
            command_dependencies.service.repair.side_effect = failure
        case "cleanup":
            command_dependencies.engine_factory.return_value.dispose.side_effect = failure
    assert repair_outbox.main([]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"Outbox repair failed at stage={stage}; details withheld.\n"
    if stage != "configuration":
        command_dependencies.engine_factory.return_value.dispose.assert_called_once_with()


@pytest.mark.parametrize(
    "result",
    [
        replace(empty_result(), unstarted=OutboxRecoveryResult(1, 0, RecoveryStopReason.DEADLINE)),
        replace(empty_result(), expired=OutboxRecoveryResult(1, 0, RecoveryStopReason.DEADLINE)),
        replace(
            empty_result(),
            unstarted=OutboxRecoveryResult(1, 0, RecoveryStopReason.SNAPSHOT_EXHAUSTED, blocked=1),
        ),
        replace(
            empty_result(),
            expired=OutboxRecoveryResult(1, 0, RecoveryStopReason.SNAPSHOT_EXHAUSTED, blocked=1),
        ),
        *[
            replace(empty_result(), delivery=OutboxDrainResult(1, 1, 0, 0, reason))
            for reason in (
                DrainStopReason.DEADLINE,
                DrainStopReason.BROKER_UNAVAILABLE,
                DrainStopReason.INVALID_PUBLICATION,
            )
        ],
        replace(empty_result(), delivery=OutboxDrainResult(1, 1, 0, 1, DrainStopReason.EMPTY)),
    ],
)
def test_incomplete_pass_reports_observations_without_claiming_success(
    result: OutboxRepairResult,
    command_dependencies: Mock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command_dependencies.service.repair.return_value = result
    assert repair_outbox.main([]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "attention_required"
    command_dependencies.service.repair.assert_called_once()


def test_target_limit_is_a_completed_bounded_pass_not_a_global_drain(
    command_dependencies: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    command_dependencies.service.repair.return_value = replace(
        empty_result(),
        delivery=OutboxDrainResult(20, 20, 20, 0, DrainStopReason.TARGET_LIMIT),
        used_targets=20,
    )
    assert repair_outbox.main([]) == 0
    assert json.loads(capsys.readouterr().out)["delivery"]["stop_reason"] == "TARGET_LIMIT"
    command_dependencies.service.repair.assert_called_once()


@pytest.mark.parametrize("profile_name", ["m1", "m0-regression"])
def test_composition_shares_worker_attempt_ceiling_and_strips_database_from_publisher(
    monkeypatch: pytest.MonkeyPatch, profile_name: str
) -> None:
    settings = OutboxRepairSettings.model_validate({"processing_profile": profile_name})
    factory = Mock()
    session_factory = Mock()
    monkeypatch.setattr(repair_outbox, "create_outbox_delivery_unit_of_work_factory", factory)
    monkeypatch.setattr(repair_outbox, "create_session_factory", session_factory)
    engine = Mock()
    service = repair_outbox.create_repair_service(settings, engine)
    profile = M1_PROCESSING_PROFILE if profile_name == "m1" else M0_0_PROCESSING_PROFILE
    factory.assert_called_once_with(
        session_factory.return_value, maximum_processing_attempts=profile.maximum_attempts
    )
    assert service.maximum_attempts == profile.maximum_attempts
    assert service.unit_of_work_factory is factory.return_value
    assert isinstance(service.publisher, BoundedCeleryOutboxPublisher)
    assert service.publisher.settings.database_url is None
    assert not hasattr(service.publisher.settings, "indexing_providers")
