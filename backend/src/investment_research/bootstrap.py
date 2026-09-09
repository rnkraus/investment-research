"""Composition root for application-owned services and adapters."""

from dataclasses import dataclass

from sqlalchemy import Engine

from investment_research.infrastructure.database.session import (
    SessionFactory,
    create_database_engine,
    create_session_factory,
)
from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.modules.documents.ports import ObjectStorage
from investment_research.modules.identity.application import (
    FixedLocalIdentityContextProvider,
    IdentityContextProvider,
)
from investment_research.settings import Settings


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    """Explicit dependencies available to API and runner entry points."""

    settings: Settings
    identity_context_provider: IdentityContextProvider
    database_engine: Engine
    session_factory: SessionFactory
    object_storage: ObjectStorage

    def close(self) -> None:
        """Release process-wide resources owned by the container."""

        self.database_engine.dispose()


def build_container(settings: Settings) -> ApplicationContainer:
    """Wire the local M0.0 adapters behind application-owned contracts."""

    database_engine = create_database_engine(settings)
    return ApplicationContainer(
        settings=settings,
        identity_context_provider=FixedLocalIdentityContextProvider(),
        database_engine=database_engine,
        session_factory=create_session_factory(database_engine),
        object_storage=LocalObjectStorage(settings.object_storage_root),
    )
