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
    *,
    maximum_processing_attempts: int | None = None,
) -> Iterator[OutboxDeliveryUnitOfWork]:
    with transactional_session(session_factory) as session:
        yield SqlAlchemyOutboxDeliveryRepositories(
            delivery=SqlAlchemyOutboxDeliveryRepository(
                session, maximum_processing_attempts=maximum_processing_attempts
            )
        )


def create_outbox_delivery_unit_of_work_factory(
    session_factory: SessionFactory,
    *,
    maximum_processing_attempts: int | None = None,
) -> OutboxDeliveryUnitOfWorkFactory:
    """Commit/close claims before the future publisher uses their detached values."""

    if maximum_processing_attempts is not None and maximum_processing_attempts < 1:
        raise ValueError("maximum_processing_attempts must be positive")

    def create_unit_of_work() -> AbstractContextManager[OutboxDeliveryUnitOfWork]:
        return sqlalchemy_outbox_delivery_unit_of_work(
            session_factory, maximum_processing_attempts=maximum_processing_attempts
        )

    return create_unit_of_work
