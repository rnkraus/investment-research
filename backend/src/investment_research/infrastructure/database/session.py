"""SQLAlchemy engine, session factory, and transaction boundaries."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from investment_research.settings import Settings

type SessionFactory = sessionmaker[Session]


def create_database_engine(settings: Settings) -> Engine:
    """Create the process-wide PostgreSQL engine without opening a connection."""

    return create_engine(
        settings.require_database_url(),
        hide_parameters=True,
        pool_pre_ping=True,
    )


def create_session_factory(engine: Engine) -> SessionFactory:
    """Bind new, non-reusable ORM sessions to the process-wide engine."""

    return sessionmaker(
        bind=engine,
        class_=Session,
        close_resets_only=False,
    )


@contextmanager
def transactional_session(session_factory: SessionFactory) -> Iterator[Session]:
    """Commit one unit of work or roll it back on any exception, then close it."""

    with session_factory.begin() as session:
        yield session
