"""Repair ticks are content-free, scope-free, explicitly invoked, and not auto-retried."""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from celery import Celery  # type: ignore[import-untyped]

from investment_research.infrastructure.queue.celery import REPAIR_OUTBOX_TASK_NAME
from investment_research.infrastructure.queue.repair_tasks import (
    InvalidOutboxRepairTickError,
    OutboxRepairTaskHandler,
    register_outbox_repair_task,
)
from investment_research.modules.delivery.contracts import OutboxRepairTickV1
from investment_research.modules.delivery.repair import ExecuteOutboxRepairService
from investment_research.modules.identity.domain import WorkspaceContext


def test_tick_carries_only_the_version_and_is_validated_before_execution() -> None:
    executor = Mock()
    tick = OutboxRepairTickV1.create()
    assert tick.model_dump(mode="json") == {"schema_version": "outbox-repair-tick.v1"}
    OutboxRepairTaskHandler(executor).handle(tick.model_dump(mode="json"))
    executor.execute_repair.assert_called_once_with()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"schema_version": "outbox-repair-tick.v2"},
        {"schema_version": "outbox-repair-tick.v1", "workspace_id": "must-not-appear"},
        {"schema_version": "outbox-repair-tick.v1", "text": "must-not-appear"},
        {"schema_version": "outbox-repair-tick.v1", "maximum_attempts": 99},
    ],
)
def test_malformed_or_scope_carrying_tick_cannot_reach_resources(payload: object) -> None:
    executor = Mock()
    with pytest.raises(
        InvalidOutboxRepairTickError, match="outbox repair tick is invalid"
    ) as error:
        OutboxRepairTaskHandler(executor).handle(payload)
    assert "must-not-appear" not in str(error.value)
    assert error.value.__cause__ is None
    executor.execute_repair.assert_not_called()


def test_registration_is_lazy_and_does_not_schedule_or_retry_tasks() -> None:
    executor = Mock()
    application = Celery("outbox-repair-test", broker="memory://")
    try:
        register_outbox_repair_task(application, executor)
        executor.execute_repair.assert_not_called()
        assert not application.conf.beat_schedule
        task = application.tasks[REPAIR_OUTBOX_TASK_NAME]
        assert task.ignore_result is True
        assert task.autoretry_for == () and task.max_retries == 0
        assert task.throws == (InvalidOutboxRepairTickError,)
        task.run(payload=OutboxRepairTickV1.create().model_dump(mode="json"))
        executor.execute_repair.assert_called_once_with()
        executor.execute_repair.reset_mock()
        with pytest.raises(InvalidOutboxRepairTickError):
            task.run(payload={"workspace_id": "must-not-appear"})
        executor.execute_repair.assert_not_called()
    finally:
        application.close()


def test_unexpected_executor_failure_propagates_without_an_automatic_retry() -> None:
    executor = Mock()
    executor.execute_repair.side_effect = RuntimeError("synthetic failure")
    application = Celery("outbox-repair-failure-test", broker="memory://")
    try:
        register_outbox_repair_task(application, executor)
        application.conf.task_always_eager = True
        result = application.tasks[REPAIR_OUTBOX_TASK_NAME].apply(
            kwargs={"payload": OutboxRepairTickV1.create().model_dump(mode="json")}, throw=False
        )
        assert result.failed()
        executor.execute_repair.assert_called_once_with()
    finally:
        application.close()


def test_execution_uses_only_the_server_owned_workspace() -> None:
    context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())
    identity = Mock()
    identity.resolve.return_value = context
    repair = Mock()
    ExecuteOutboxRepairService(repair, identity).execute_repair()
    identity.resolve.assert_called_once_with()
    repair.repair.assert_called_once_with(context)


def test_identity_failure_prevents_repair() -> None:
    identity = Mock()
    identity.resolve.side_effect = RuntimeError("synthetic identity failure")
    repair = Mock()
    with pytest.raises(RuntimeError, match="synthetic identity failure"):
        ExecuteOutboxRepairService(repair, identity).execute_repair()
    repair.repair.assert_not_called()
