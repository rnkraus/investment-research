"""FastAPI dependencies that expose server-owned application context."""

from typing import Annotated

from fastapi import Depends, Request

from investment_research.bootstrap import ApiContainer
from investment_research.modules.documents.application import (
    CaptureDocumentService,
    OpenOriginalService,
)
from investment_research.modules.evidence.application import ResolveEvidenceSpanService
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import GetProcessingRunService
from investment_research.modules.research.application import AnswerResearchRequestService


def get_container(request: Request) -> ApiContainer:
    """Return the container installed by the application factory."""

    container = getattr(request.app.state, "container", None)
    if not isinstance(container, ApiContainer):
        raise RuntimeError("application container is not configured")
    return container


ContainerDependency = Annotated[ApiContainer, Depends(get_container)]


def get_capture_document_service(
    container: ContainerDependency,
) -> CaptureDocumentService:
    """Return the application-owned service for immutable document capture."""

    return container.capture_document_service


CaptureDocumentServiceDependency = Annotated[
    CaptureDocumentService,
    Depends(get_capture_document_service),
]


def get_open_original_service(
    container: ContainerDependency,
) -> OpenOriginalService:
    """Return the application-owned service for verified original access."""

    return container.open_original_service


OpenOriginalServiceDependency = Annotated[
    OpenOriginalService,
    Depends(get_open_original_service),
]


def get_resolve_evidence_span_service(
    container: ContainerDependency,
) -> ResolveEvidenceSpanService:
    """Return the application-owned service for authorized citation reads."""

    return container.resolve_evidence_span_service


ResolveEvidenceSpanServiceDependency = Annotated[
    ResolveEvidenceSpanService,
    Depends(get_resolve_evidence_span_service),
]


def get_processing_run_service(
    container: ContainerDependency,
) -> GetProcessingRunService:
    """Return the application-owned service for scoped processing-status reads."""

    return container.get_processing_run_service


GetProcessingRunServiceDependency = Annotated[
    GetProcessingRunService,
    Depends(get_processing_run_service),
]


def get_answer_research_request_service(
    container: ContainerDependency,
) -> AnswerResearchRequestService:
    """Return the complete validated research-request use case."""

    return container.answer_research_request_service


AnswerResearchRequestServiceDependency = Annotated[
    AnswerResearchRequestService,
    Depends(get_answer_research_request_service),
]


def get_workspace_context(container: ContainerDependency) -> WorkspaceContext:
    """Resolve workspace scope from server-controlled state only."""

    return container.identity_context_provider.resolve()


WorkspaceContextDependency = Annotated[WorkspaceContext, Depends(get_workspace_context)]
