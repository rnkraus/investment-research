"""FastAPI composition root for the M0.0 application."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from investment_research import __version__
from investment_research.api.errors import (
    ApiError,
    api_error_handler,
    request_validation_error_handler,
)
from investment_research.api.middleware import RequestBodyLimitMiddleware, RequestLoggingMiddleware
from investment_research.api.openapi import install_openapi
from investment_research.api.routes.document_deletions import router as document_deletions_router
from investment_research.api.routes.documents import router as documents_router
from investment_research.api.routes.evidence import router as evidence_router
from investment_research.api.routes.health import router as health_router
from investment_research.api.routes.processing import router as processing_router
from investment_research.api.routes.research import router as research_router
from investment_research.bootstrap import build_api_container
from investment_research.infrastructure.logging import configure_logging
from investment_research.settings import ApiSettings, get_api_settings


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    """Create the API with explicit dependencies and no runtime schema mutation."""

    resolved_settings = settings or get_api_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configure_logging(process_name="api", level=resolved_settings.log_level)
        container = build_api_container(resolved_settings)
        application.state.container = container
        try:
            yield
        finally:
            container.close()
            del application.state.container

    application = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        description="Local-only M0.0 API for evidence-grounded investment research.",
        lifespan=lifespan,
    )
    application.add_exception_handler(ApiError, api_error_handler)
    application.add_exception_handler(
        RequestValidationError,
        request_validation_error_handler,
    )
    application.include_router(documents_router)
    application.include_router(document_deletions_router)
    application.include_router(evidence_router)
    application.include_router(processing_router)
    application.include_router(research_router)
    application.include_router(health_router)
    install_openapi(application)
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_upload_bytes=resolved_settings.max_upload_bytes,
    )
    application.add_middleware(
        RequestLoggingMiddleware,
        routes=tuple(application.routes),
    )
    return application
