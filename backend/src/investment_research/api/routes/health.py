"""Process health endpoint."""

from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict

from investment_research.api.dependencies import ContainerDependency

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Public health response without dependency or credential details."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    service: str
    version: str


class ReadinessResponse(BaseModel):
    """Public readiness result without dependency or failure details."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ready", "not_ready"]


@router.get("/health", response_model=HealthResponse)
def read_health(request: Request, response: Response) -> HealthResponse:
    """Report process liveness without touching runtime dependencies."""

    response.headers["cache-control"] = "no-store"
    return HealthResponse(service=request.app.title, version=request.app.version)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
def read_readiness(
    response: Response,
    container: ContainerDependency,
) -> ReadinessResponse:
    """Report whether every API dependency can safely serve traffic."""

    response.headers["cache-control"] = "no-store"
    if not container.readiness_probe.is_ready():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="not_ready")
    return ReadinessResponse(status="ready")
