"""Strict company HTTP contracts and bounded canonical continuation encoding."""

from base64 import b64decode, urlsafe_b64encode
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from investment_research.modules.companies.contracts import (
    CompanyListCursor,
    CompanyListQuery,
    CompanySummary,
    CreateCompanyCommand,
)
from investment_research.modules.companies.domain import CompanyName
from investment_research.modules.companies.ports import InvalidCompanyCursorError

COMPANY_CURSOR_MAX_LENGTH = 4096
CompanyCursorToken = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=COMPANY_CURSOR_MAX_LENGTH,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]


class CompanyInput(CreateCompanyCommand):
    """Accept a display name and optional ticker with server-owned identity and scope."""


class CompanyCatalogQuery(BaseModel):
    """Public query fields without internal cursor fields or client-owned scope."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    q: CompanyName | None = None
    page_size: int = Field(default=20, ge=1, le=100)
    cursor: CompanyCursorToken | None = None

    @field_validator("q", mode="before")
    @classmethod
    def normalize_search(cls, value: object) -> str | None:
        """Reuse the application search rules, including blank-as-absent behavior."""

        return CompanyListQuery.model_validate({"q": value}).q


class CompanyCatalogPage(BaseModel):
    """Expose safe summaries and an opaque position without internal comparison keys."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    items: tuple[CompanySummary, ...] = Field(max_length=100)
    next_cursor: CompanyCursorToken | None = None


def encode_company_cursor(cursor: CompanyListCursor) -> str:
    """Encode only the version, canonical anchor UUID, and query fingerprint."""

    return urlsafe_b64encode(cursor.model_dump_json().encode("utf-8")).decode("ascii").rstrip("=")


def decode_company_cursor(token: str) -> CompanyListCursor:
    """Bound parsing and reject malformed, duplicate-field, or noncanonical tokens uniformly."""

    if not token or len(token) > COMPANY_CURSOR_MAX_LENGTH:
        raise InvalidCompanyCursorError("company cursor is invalid")
    try:
        raw = b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        cursor = CompanyListCursor.model_validate_json(raw)
        if encode_company_cursor(cursor) != token:
            raise ValueError("noncanonical company cursor")
    except ValueError:
        raise InvalidCompanyCursorError("company cursor is invalid") from None
    return cursor
