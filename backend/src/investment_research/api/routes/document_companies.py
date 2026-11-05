"""Read and replace a document's company assignments in the active workspace."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.contracts import ErrorCode, ErrorResponse
from investment_research.api.dependencies import (
    DocumentCompanyServiceDependency,
    WorkspaceContextDependency,
)
from investment_research.api.errors import ApiError
from investment_research.modules.companies.domain import CompanyNameIntegrityError
from investment_research.modules.documents.company_assignment import (
    CompanyAssignmentConflictError,
    CompanyAssignmentUnavailableError,
    DocumentCompanyAssignment,
    ReplaceDocumentCompaniesCommand,
)


def require_no_query(request: Request) -> None:
    if request.query_params:
        raise ApiError(
            status_code=422, code=ErrorCode.INVALID_REQUEST, message="unexpected query parameters"
        )


router = APIRouter(
    prefix="/api/v1/documents/{document_id}/companies",
    tags=["documents"],
    dependencies=[Depends(require_no_query)],
)


@contextmanager
def translate_assignment_errors() -> Iterator[None]:
    try:
        yield
    except CompanyAssignmentUnavailableError:
        raise ApiError(
            status_code=404, code=ErrorCode.NOT_FOUND, message="assignment target is unavailable"
        ) from None
    except CompanyAssignmentConflictError:
        raise ApiError(
            status_code=409, code=ErrorCode.CONFLICT, message="company assignment has changed"
        ) from None
    except SQLAlchemyError, CompanyNameIntegrityError, ValidationError:
        raise ApiError(
            status_code=500,
            code=ErrorCode.INTERNAL_ERROR,
            message="company assignment request failed",
        ) from None


@router.get("", responses={code: {"model": ErrorResponse} for code in (404, 422, 500)})
def get_document_companies(
    document_id: UUID,
    service: DocumentCompanyServiceDependency,
    context: WorkspaceContextDependency,
) -> DocumentCompanyAssignment:
    """Read the current company set and the version and revision needed for an edit."""

    with translate_assignment_errors():
        return service.get(context, document_id)


@router.put("", responses={code: {"model": ErrorResponse} for code in (404, 409, 413, 422, 500)})
def replace_document_companies(
    document_id: UUID,
    payload: ReplaceDocumentCompaniesCommand,
    service: DocumentCompanyServiceDependency,
    context: WorkspaceContextDependency,
) -> DocumentCompanyAssignment:
    """Replace all company assignments after checking the observed version and revision."""

    with translate_assignment_errors():
        return service.replace(context, document_id, payload)
