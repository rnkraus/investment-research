"""Application-owned input and safe summary contracts for company creation."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator

from investment_research.modules.companies.domain import CompanyName


class CreateCompanyCommand(BaseModel):
    """Accept a name while leaving identity, actor, and workspace to the server."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    name: CompanyName


class CompanySummary(BaseModel):
    """Expose company identity and display metadata without internal comparison keys."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    company_id: UUID
    name: CompanyName
    created_at: AwareDatetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Expose the creation instant in canonical UTC."""

        return value.astimezone(UTC)
