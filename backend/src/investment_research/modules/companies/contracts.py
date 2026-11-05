"""Application-owned input and safe summary contracts for company creation."""

import json
import unicodedata
from datetime import UTC, datetime
from hashlib import sha256
from typing import Final, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from investment_research.modules.companies.domain import (
    Company,
    CompanyName,
    CompanyTicker,
    company_name_key,
)

COMPANY_LIST_ORDER_VERSION: Final = "companies-name-v1"


class CreateCompanyCommand(BaseModel):
    """Accept display metadata while leaving identity, actor, and workspace to the server."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    name: CompanyName
    ticker: CompanyTicker | None = None


class UpdateCompanyTickerCommand(BaseModel):
    """Replace the optional ticker with a symbol or an explicit null."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    ticker: CompanyTicker | None


class CompanySummary(BaseModel):
    """Expose company identity and display metadata without internal comparison keys."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    company_id: UUID
    name: CompanyName
    ticker: CompanyTicker | None = None
    created_at: AwareDatetime

    @classmethod
    def from_company(cls, company: Company) -> Self:
        return cls(
            company_id=company.id,
            name=company.name,
            ticker=company.ticker,
            created_at=company.created_at,
        )

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Expose the creation instant in canonical UTC."""

        return value.astimezone(UTC)


class CompanyListCursor(BaseModel):
    """An untrusted continuation position containing no company name or search text."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    version: Literal["companies-name-v1"] = COMPANY_LIST_ORDER_VERSION
    company_id: UUID
    query_fingerprint: str = Field(
        strict=True, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"
    )


class CompanyListQuery(BaseModel):
    """Bound literal name discovery without accepting a client-owned workspace."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    q: CompanyName | None = None
    page_size: int = Field(default=20, ge=1, le=100, strict=True)
    after: CompanyListCursor | None = None

    @field_validator("q", mode="before")
    @classmethod
    def normalize_blank_query(cls, value: object) -> object:
        """Treat blank text as absent while preserving name rules against control characters."""

        if isinstance(value, str) and not value.strip():
            if any(unicodedata.category(character) == "Cc" for character in value):
                raise ValueError("company search contains unsupported characters")
            return None
        return value

    @property
    def normalized_query(self) -> str | None:
        """Match the same Unicode comparison semantics as stored company names."""

        return None if self.q is None else company_name_key(self.q)

    def fingerprint(self, workspace_id: UUID) -> str:
        """Bind a position to trusted scope, normalized filters, size, and fixed ordering."""

        payload = json.dumps(
            [COMPANY_LIST_ORDER_VERSION, str(workspace_id), self.normalized_query, self.page_size],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return sha256(payload.encode("utf-8")).hexdigest()


class CompanyListPage(BaseModel):
    """Return safe bounded summaries and an optional continuation position."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    items: tuple[CompanySummary, ...] = Field(max_length=100)
    next_cursor: CompanyListCursor | None = None
