"""Composition root for application-owned services and adapters."""

from dataclasses import dataclass

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


def build_container(settings: Settings) -> ApplicationContainer:
    """Wire the local M0.0 adapters behind application-owned contracts."""

    return ApplicationContainer(
        settings=settings,
        identity_context_provider=FixedLocalIdentityContextProvider(),
    )
