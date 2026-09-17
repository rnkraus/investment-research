"""HTTP endpoint for resolving authorized citation spans."""

from uuid import UUID

from fastapi import APIRouter, Response, status
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.contracts import ErrorCode, ErrorResponse, EvidenceSpanResult
from investment_research.api.dependencies import (
    ResolveEvidenceSpanServiceDependency,
    WorkspaceContextDependency,
)
from investment_research.api.errors import ApiError
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.evidence.application import EvidenceSpanUnavailableError

router = APIRouter(prefix="/api/v1/evidence-spans", tags=["evidence"])


@router.get(
    "/{evidence_span_id}",
    response_model=EvidenceSpanResult,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
    },
)
def resolve_evidence_span(
    evidence_span_id: UUID,
    service: ResolveEvidenceSpanServiceDependency,
    context: WorkspaceContextDependency,
    response: Response,
) -> EvidenceSpanResult:
    """Return an exact current citation location without storage implementation details."""

    try:
        resolved = service.resolve(context, evidence_span_id)
    except EvidenceSpanUnavailableError as error:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ErrorCode.NOT_FOUND,
            message="evidence span was not found",
        ) from error
    except (SQLAlchemyError, ValidationError) as error:
        raise ApiError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
            message="evidence span could not be resolved",
        ) from error

    span = resolved.span
    if span.source_locator_kind is not SourceLocatorKind.PDF_PAGE or span.page_number is None:
        raise ApiError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
            message="evidence span locator is not supported",
        )
    response.headers["cache-control"] = "private, no-store"
    return EvidenceSpanResult(
        evidence_span_id=span.id,
        document_id=resolved.document_id,
        document_version_id=span.document_version_id,
        page=span.page_number,
        start_character=span.start_character,
        end_character=span.end_character,
        exact_text=span.exact_text,
        text_sha256=span.text_sha256,
        viewer_url=(
            f"/api/v1/documents/{resolved.document_id}/versions/"
            f"{span.document_version_id}/original#page={span.page_number}"
        ),
    )
