"""HTTP endpoint for secure document capture."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from pydantic import AwareDatetime, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.contracts import (
    DocumentAccepted,
    DocumentMetadataInput,
    ErrorCode,
    ErrorResponse,
    LanguageCode,
    TrimmedTitle,
)
from investment_research.api.dependencies import (
    CaptureDocumentServiceDependency,
    WorkspaceContextDependency,
)
from investment_research.api.errors import ApiError
from investment_research.modules.documents.application import (
    InvalidPdfSignatureError,
    UnsupportedDocumentTypeError,
    UploadTooLargeError,
)
from investment_research.modules.documents.contracts import CaptureDocumentInput
from investment_research.modules.documents.ports import (
    ObjectAlreadyExistsError,
    ObjectStorageError,
)

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


def document_metadata_from_form(
    title: Annotated[TrimmedTitle, Form()],
    language: Annotated[LanguageCode, Form()] = "en",
    published_at: Annotated[AwareDatetime | None, Form()] = None,
    available_at: Annotated[AwareDatetime | None, Form()] = None,
) -> DocumentMetadataInput:
    """Assemble validated application-neutral metadata from multipart fields."""

    return DocumentMetadataInput(
        title=title,
        language=language,
        published_at=published_at,
        available_at=available_at,
    )


DocumentMetadataDependency = Annotated[
    DocumentMetadataInput,
    Depends(document_metadata_from_form),
]


@router.post(
    "",
    response_model=DocumentAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
        status.HTTP_413_CONTENT_TOO_LARGE: {"model": ErrorResponse},
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"model": ErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
    },
)
def capture_document(
    service: CaptureDocumentServiceDependency,
    context: WorkspaceContextDependency,
    metadata: DocumentMetadataDependency,
    file: Annotated[
        UploadFile,
        File(description="English PDF original to preserve and process"),
    ],
) -> DocumentAccepted:
    """Accept one bounded PDF and return its canonical capture identifiers."""

    try:
        capture_input = CaptureDocumentInput(
            filename=file.filename or "",
            media_type=file.content_type or "",
            title=metadata.title,
            language=metadata.language,
            published_at=metadata.published_at,
            available_at=metadata.available_at,
        )
        result = service.capture(context, capture_input, file.file)
    except ValidationError as error:
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ErrorCode.INVALID_REQUEST,
            message="uploaded file metadata is invalid",
        ) from error
    except UploadTooLargeError as error:
        raise ApiError(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            message="uploaded PDF exceeds the configured size limit",
        ) from error
    except (UnsupportedDocumentTypeError, InvalidPdfSignatureError) as error:
        raise ApiError(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            code=ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            message="uploaded file is not a supported PDF",
        ) from error
    except ObjectAlreadyExistsError as error:
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code=ErrorCode.CONFLICT,
            message="immutable original already exists",
        ) from error
    except (ObjectStorageError, SQLAlchemyError) as error:
        raise ApiError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
            message="document capture failed",
        ) from error

    return DocumentAccepted(
        document_id=result.document_id,
        document_version_id=result.document_version_id,
        processing_run_id=result.processing_run_id,
        processing_status_url=f"/api/v1/processing-runs/{result.processing_run_id}",
    )
