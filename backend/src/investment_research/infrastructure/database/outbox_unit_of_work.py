"""Short outbox transactions, intentionally separate from broker operations."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

from investment_research.infrastructure.database.outbox_delivery import (
    SqlAlchemyOutboxDeliveryRepository,
)
from investment_research.infrastructure.database.session import (
    SessionFactory,
    transactional_session,
)
from investment_research.modules.delivery.ports import (
    OutboxDeliveryRepository,
    OutboxDeliveryUnitOfWork,
    OutboxDeliveryUnitOfWorkFactory,
)


@dataclass(frozen=True, slots=True)
class SqlAlchemyOutboxDeliveryRepositories:
    delivery: OutboxDeliveryRepository


@contextmanager
def sqlalchemy_outbox_delivery_unit_of_work(
    session_factory: SessionFactory,
) -> Iterator[OutboxDeliveryUnitOfWork]:
    with transactional_session(session_factory) as session:
        yield SqlAlchemyOutboxDeliveryRepositories(
            delivery=SqlAlchemyOutboxDeliveryRepository(session)
        )


def create_outbox_delivery_unit_of_work_factory(
    session_factory: SessionFactory,
) -> OutboxDeliveryUnitOfWorkFactory:
    """Commit/close claims before the future publisher uses their detached values."""

    def create_unit_of_work() -> AbstractContextManager[OutboxDeliveryUnitOfWork]:
        return sqlalchemy_outbox_delivery_unit_of_work(session_factory)

    return create_unit_of_work
