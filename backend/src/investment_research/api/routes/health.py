"""Process health endpoint."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from investment_research import __version__
from investment_research.api.dependencies import ContainerDependency

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Public health response without dependency or credential details."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    service: str
    version: str


@router.get("/health", response_model=HealthResponse)
def read_health(container: ContainerDependency) -> HealthResponse:
    """Report API process liveness."""

    return HealthResponse(service=container.settings.app_name, version=__version__)
