"""Explicit permanent-deletion admission and content-free scoped status reads."""

from uuid import UUID

from fastapi import APIRouter, Response, status
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.contracts import (
    DocumentDeletionInput,
    DocumentDeletionResult,
    DocumentDeletionsAccepted,
    ErrorCode,
    ErrorResponse,
)
from investment_research.api.dependencies import (
    GetDocumentDeletionServiceDependency,
    SubmitDocumentDeletionServiceDependency,
    WorkspaceContextDependency,
)
from investment_research.api.errors import ApiError
from investment_research.infrastructure.logging import application_logger
from investment_research.modules.documents.deletion_contracts import (
    DeleteDocumentsCommand,
    DocumentDeletionReceipt,
)
from investment_research.modules.documents.deletion_ports import (
    DocumentDeletionBusyError,
    DocumentDeletionUnavailableError,
    DocumentPurgePendingError,
)

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])
logger = application_logger(__name__)


def deletion_result(receipt: DocumentDeletionReceipt) -> DocumentDeletionResult:
    return DocumentDeletionResult(
        document_id=receipt.document_id,
        state=receipt.state,
        requested_at=receipt.requested_at,
        completed_at=receipt.completed_at,
        status_url=f"/api/v1/documents/{receipt.document_id}/deletion",
    )


@router.post(
    "/deletions",
    response_model=DocumentDeletionsAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
    },
)
def request_document_deletions(
    payload: DocumentDeletionInput,
    service: SubmitDocumentDeletionServiceDependency,
    context: WorkspaceContextDependency,
    response: Response,
) -> DocumentDeletionsAccepted:
    """Exclude the whole confirmed batch before best-effort post-commit publication."""

    try:
        result = service.submit(context, DeleteDocumentsCommand(document_ids=payload.document_ids))
    except DocumentDeletionUnavailableError:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ErrorCode.NOT_FOUND,
            message="document deletion is unavailable",
        ) from None
    except DocumentDeletionBusyError:
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code=ErrorCode.CONFLICT,
            message="finish document processing before deletion",
        ) from None
    except DocumentPurgePendingError, SQLAlchemyError:
        raise ApiError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
            message="document deletion could not be accepted; check status before retrying",
        ) from None
    if result.dispatch_pending_document_ids:
        logger.warning(
            "document.purge.dispatch.failed",
            extra={"pending_count": len(result.dispatch_pending_document_ids)},
        )
    response.headers["cache-control"] = "private, no-store"
    return DocumentDeletionsAccepted(
        items=tuple(deletion_result(receipt) for receipt in result.receipts),
        dispatch_pending_document_ids=result.dispatch_pending_document_ids,
    )


@router.get(
    "/{document_id}/deletion",
    response_model=DocumentDeletionResult,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
    },
)
def get_document_deletion(
    document_id: UUID,
    service: GetDocumentDeletionServiceDependency,
    context: WorkspaceContextDependency,
    response: Response,
) -> DocumentDeletionResult:
    """Return a receipt after removal without disclosing foreign or unknown targets."""

    try:
        receipt = service.get(context, document_id)
    except DocumentDeletionUnavailableError:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ErrorCode.NOT_FOUND,
            message="document deletion is unavailable",
        ) from None
    except DocumentPurgePendingError, SQLAlchemyError:
        raise ApiError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
            message="document deletion status could not be read",
        ) from None
    response.headers["cache-control"] = "private, no-store"
    return deletion_result(receipt)
