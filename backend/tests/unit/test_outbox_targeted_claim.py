"""Exact outbox selection must retain workspace qualification and single-row locks."""

from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.orm import Session

from investment_research.infrastructure.database.outbox_delivery import (
    SqlAlchemyOutboxDeliveryRepository,
)
from investment_research.modules.delivery.domain import OutboxTransitionError
from investment_research.modules.identity.domain import WorkspaceContext


def test_targeted_query_adds_exact_id_and_preserves_workspace_limit_and_skip_locked() -> None:
    session = Mock(spec=Session)
    session.execute.return_value.one_or_none.return_value = None
    context = WorkspaceContext(workspace_id=uuid4(), user_id=uuid4())
    identifier = uuid4()
    repository = SqlAlchemyOutboxDeliveryRepository(session)
    assert repository.claim_due_event(context, event_id=identifier, now=datetime.now(UTC)) is None
    session.execute.assert_called_once()
    # SQLAlchemy's dialect constructor is untyped; the compiled statement stays inspectable.
    dialect = PGDialect()  # type: ignore[no-untyped-call]
    statement = session.execute.call_args.args[0].compile(dialect=dialect)
    values = tuple(statement.params.values())
    assert identifier in values and context.workspace_id in values
    sql = str(statement)
    assert "LIMIT" in sql and "SKIP LOCKED" in sql
    assert "FOR UPDATE OF outbox_events, processing_runs" in sql


@pytest.mark.parametrize("targeted", [False, True])
def test_both_claim_entry_points_reject_naive_time_before_query(targeted: bool) -> None:
    session = Mock(spec=Session)
    context = WorkspaceContext(workspace_id=uuid4(), user_id=uuid4())
    repository = SqlAlchemyOutboxDeliveryRepository(session)
    with pytest.raises(OutboxTransitionError):
        if targeted:
            repository.claim_due_event(context, event_id=uuid4(), now=datetime(2026, 10, 22))
        else:
            repository.claim_next_due(context, now=datetime(2026, 10, 22))
    session.execute.assert_not_called()
