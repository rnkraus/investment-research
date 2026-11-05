"""Workspace-scoped company creation and bounded literal-name discovery."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.company_contracts import (
    CompanyCatalogPage,
    CompanyCatalogQuery,
    CompanyInput,
    decode_company_cursor,
    encode_company_cursor,
)
from investment_research.api.contracts import ErrorCode, ErrorResponse
from investment_research.api.dependencies import (
    CompanyServiceDependency,
    WorkspaceContextDependency,
)
from investment_research.api.errors import ApiError
from investment_research.modules.companies.application import CompanyUnavailableError
from investment_research.modules.companies.contracts import (
    CompanyListQuery,
    CompanySummary,
    CreateCompanyCommand,
    UpdateCompanyTickerCommand,
)
from investment_research.modules.companies.domain import (
    CompanyNameIntegrityError,
    CompanyScopeError,
)
from investment_research.modules.companies.ports import (
    CompanyCommitUncertainError,
    CompanyNameConflictError,
    InvalidCompanyCursorError,
)


def require_single_query_parameters(request: Request) -> None:
    """Reject repeated scalar query fields instead of choosing an ambiguous last value."""

    if (request.method in {"POST", "PATCH"} and request.query_params) or any(
        len(request.query_params.getlist(key)) != 1 for key in request.query_params
    ):
        raise ApiError(
            status_code=422, code=ErrorCode.INVALID_REQUEST, message="company query is invalid"
        )


router = APIRouter(
    prefix="/api/v1/companies",
    tags=["companies"],
    dependencies=[Depends(require_single_query_parameters)],
)


@router.post(
    "",
    response_model=CompanySummary,
    status_code=status.HTTP_201_CREATED,
    responses={code: {"model": ErrorResponse} for code in (409, 413, 422, 500)},
)
def create_company(
    payload: CompanyInput,
    service: CompanyServiceDependency,
    context: WorkspaceContextDependency,
) -> CompanySummary:
    """Create one canonical company after commit, without provider or queue work."""

    try:
        return service.create(
            context, CreateCompanyCommand(name=payload.name, ticker=payload.ticker)
        )
    except CompanyNameConflictError:
        raise ApiError(
            status_code=409, code=ErrorCode.CONFLICT, message="company name already exists"
        ) from None
    except (
        SQLAlchemyError,
        CompanyCommitUncertainError,
        CompanyScopeError,
        CompanyNameIntegrityError,
    ):
        raise ApiError(
            status_code=500, code=ErrorCode.INTERNAL_ERROR, message="company could not be created"
        ) from None


@router.patch(
    "/{company_id}/ticker",
    responses={code: {"model": ErrorResponse} for code in (404, 413, 422, 500)},
)
def update_company_ticker(
    company_id: UUID,
    payload: UpdateCompanyTickerCommand,
    service: CompanyServiceDependency,
    context: WorkspaceContextDependency,
) -> CompanySummary:
    """Set or clear the ticker of an existing company in the active workspace."""

    try:
        return service.update_ticker(context, company_id, payload)
    except CompanyUnavailableError:
        raise ApiError(
            status_code=404, code=ErrorCode.NOT_FOUND, message="company is unavailable"
        ) from None
    except SQLAlchemyError, CompanyCommitUncertainError, CompanyNameIntegrityError:
        raise ApiError(
            status_code=500, code=ErrorCode.INTERNAL_ERROR, message="ticker could not be saved"
        ) from None


@router.get(
    "",
    response_model=CompanyCatalogPage,
    responses={code: {"model": ErrorResponse} for code in (422, 500)},
)
def list_companies(
    query: Annotated[CompanyCatalogQuery, Query()],
    service: CompanyServiceDependency,
    context: WorkspaceContextDependency,
) -> CompanyCatalogPage:
    """List one scoped page and translate an untrusted continuation into internal contracts."""

    try:
        after = None if query.cursor is None else decode_company_cursor(query.cursor)
        page = service.list_companies(
            context, CompanyListQuery(q=query.q, page_size=query.page_size, after=after)
        )
    except InvalidCompanyCursorError:
        raise ApiError(
            status_code=422, code=ErrorCode.INVALID_REQUEST, message="company cursor is invalid"
        ) from None
    except SQLAlchemyError, CompanyNameIntegrityError:
        raise ApiError(
            status_code=500, code=ErrorCode.INTERNAL_ERROR, message="company list could not be read"
        ) from None
    return CompanyCatalogPage(
        items=page.items,
        next_cursor=None if page.next_cursor is None else encode_company_cursor(page.next_cursor),
    )
