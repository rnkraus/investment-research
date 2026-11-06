"""Provider-free, workspace-scoped discovery of indexed passages."""

from fastapi import APIRouter
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.contracts import ErrorCode, ErrorResponse
from investment_research.api.dependencies import (
    SearchArchiveServiceDependency,
    WorkspaceContextDependency,
)
from investment_research.api.errors import ApiError
from investment_research.modules.search.archive import ArchiveSearchInput, ArchiveSearchResult

router = APIRouter(prefix="/api/v1/archive-search", tags=["archive-search"])


@router.post("", responses={code: {"model": ErrorResponse} for code in (413, 422, 500)})
def search_archive(
    command: ArchiveSearchInput,
    service: SearchArchiveServiceDependency,
    context: WorkspaceContextDependency,
) -> ArchiveSearchResult:
    """Return lexical passage matches without requesting an AI answer."""

    try:
        return service.search(context, command)
    except SQLAlchemyError:
        raise ApiError(
            status_code=500, code=ErrorCode.INTERNAL_ERROR, message="archive search could not run"
        ) from None
