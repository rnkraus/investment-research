"""HTTP endpoint for workspace-scoped processing status."""

from uuid import UUID

from fastapi import APIRouter, Response, status
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.contracts import ErrorCode, ErrorResponse, ProcessingRunResult
from investment_research.api.dependencies import (
    GetProcessingRunServiceDependency,
    WorkspaceContextDependency,
)
from investment_research.api.errors import ApiError
from investment_research.modules.processing.application import ProcessingRunUnavailableError

router = APIRouter(prefix="/api/v1/processing-runs", tags=["processing"])


@router.get(
    "/{processing_run_id}",
    response_model=ProcessingRunResult,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
    },
)
def get_processing_run(
    processing_run_id: UUID,
    service: GetProcessingRunServiceDependency,
    context: WorkspaceContextDependency,
    response: Response,
) -> ProcessingRunResult:
    """Return safe status for one run without disclosing inaccessible identifiers."""

    try:
        run = service.get(context, processing_run_id)
    except ProcessingRunUnavailableError as error:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ErrorCode.NOT_FOUND,
            message="processing run was not found",
        ) from error
    except SQLAlchemyError as error:
        raise ApiError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
            message="processing status could not be read",
        ) from error

    response.headers["cache-control"] = "private, no-store"
    return ProcessingRunResult(
        processing_run_id=run.id,
        document_version_id=run.document_version_id,
        state=run.state,
        error_summary=run.error_summary,
    )
