"""FastAPI dependencies that expose server-owned application context."""

from typing import Annotated

from fastapi import Depends, Request

from investment_research.bootstrap import ApplicationContainer
from investment_research.modules.documents.application import CaptureDocumentService
from investment_research.modules.identity.domain import WorkspaceContext


def get_container(request: Request) -> ApplicationContainer:
    """Return the container installed by the application factory."""

    container = getattr(request.app.state, "container", None)
    if not isinstance(container, ApplicationContainer):
        raise RuntimeError("application container is not configured")
    return container


ContainerDependency = Annotated[ApplicationContainer, Depends(get_container)]


def get_capture_document_service(
    container: ContainerDependency,
) -> CaptureDocumentService:
    """Return the application-owned service for immutable document capture."""

    return container.capture_document_service


CaptureDocumentServiceDependency = Annotated[
    CaptureDocumentService,
    Depends(get_capture_document_service),
]


def get_workspace_context(container: ContainerDependency) -> WorkspaceContext:
    """Resolve workspace scope from server-controlled state only."""

    return container.identity_context_provider.resolve()


WorkspaceContextDependency = Annotated[WorkspaceContext, Depends(get_workspace_context)]
