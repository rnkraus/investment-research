"""Workspace-scoped discovery of indexed passages."""

from fastapi import APIRouter
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.contracts import ErrorCode, ErrorResponse
from investment_research.api.dependencies import (
    SearchArchiveServiceDependency,
    WorkspaceContextDependency,
)
from investment_research.api.errors import ApiError
from investment_research.modules.operations.application import (
    ProviderBudgetExceededError,
    ProviderUsageExceededReservationError,
)
from investment_research.modules.search.application import EmbeddingResponseContractError
from investment_research.modules.search.archive import ArchiveSearchInput, ArchiveSearchResult
from investment_research.modules.search.ports import SearchProviderError

router = APIRouter(prefix="/api/v1/archive-search", tags=["archive-search"])


@router.post("", responses={code: {"model": ErrorResponse} for code in (413, 422, 429, 500, 503)})
def search_archive(
    command: ArchiveSearchInput,
    service: SearchArchiveServiceDependency,
    context: WorkspaceContextDependency,
) -> ArchiveSearchResult:
    """Return keyword or semantic matches without requesting an AI answer."""

    try:
        return service.search(context, command)
    except ProviderBudgetExceededError:
        raise ApiError(
            status_code=429,
            code=ErrorCode.PROVIDER_UNAVAILABLE,
            message="workspace provider budget exhausted",
        ) from None
    except (
        SearchProviderError,
        EmbeddingResponseContractError,
        ProviderUsageExceededReservationError,
    ):
        raise ApiError(
            status_code=503,
            code=ErrorCode.PROVIDER_UNAVAILABLE,
            message="semantic archive search is unavailable",
        ) from None
    except SQLAlchemyError:
        raise ApiError(
            status_code=500, code=ErrorCode.INTERNAL_ERROR, message="archive search could not run"
        ) from None
