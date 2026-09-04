"""Process health endpoint."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from investment_research.settings import Settings, get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Public health response without dependency or credential details."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    service: str
    version: str


@router.get("/health", response_model=HealthResponse)
def read_health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """Report API process liveness."""

    return HealthResponse(service=settings.app_name, version="0.0.0")
