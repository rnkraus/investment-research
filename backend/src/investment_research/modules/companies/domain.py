"""Company identity and bounded name normalization without provider dependencies."""

import unicodedata
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StringConstraints,
    field_validator,
)

COMPANY_NAME_MAX_LENGTH = 200
COMPANY_NAME_KEY_MAX_LENGTH = 600
COMPANY_TICKER_MAX_LENGTH = 20
_UNSUPPORTED_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})


class CompanyScopeError(ValueError):
    """Reject a company write outside the trusted workspace or creator context."""


class CompanyNameIntegrityError(ValueError):
    """Reject stored comparison data that disagrees with the canonical company name."""


def normalize_company_name(value: object) -> str:
    """Normalize display spelling while rejecting hidden controls and invalid lengths."""

    if not isinstance(value, str):
        raise ValueError("company name must be text")
    if any(unicodedata.category(character) in _UNSUPPORTED_NAME_CATEGORIES for character in value):
        raise ValueError("company name contains unsupported characters")
    name = " ".join(unicodedata.normalize("NFC", value).split())
    if not 1 <= len(name) <= COMPANY_NAME_MAX_LENGTH:
        raise ValueError("company name must contain between 1 and 200 characters")
    return name


CompanyName = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=COMPANY_NAME_MAX_LENGTH),
    BeforeValidator(normalize_company_name),
]


def normalize_company_ticker(value: object) -> str:
    if not isinstance(value, str) or not value.isascii():
        raise ValueError("company ticker must use ASCII characters")
    return value.strip(" ").upper()


CompanyTicker = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=COMPANY_TICKER_MAX_LENGTH,
        pattern=r"^[A-Z0-9][A-Z0-9.-]*$",
    ),
    BeforeValidator(normalize_company_ticker),
]


def company_name_key(name: CompanyName) -> str:
    """Derive a case-insensitive key without stripping accents or punctuation."""

    return normalize_company_name(name).casefold()


class Company(BaseModel):
    """Workspace-owned economic entity, independent of any exchange-specific ticker."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    id: UUID
    workspace_id: UUID
    name: CompanyName
    ticker: CompanyTicker | None = None
    created_by: UUID
    created_at: AwareDatetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Keep the recorded creation instant in UTC."""

        return value.astimezone(UTC)

    @property
    def normalized_name(self) -> str:
        """Compute the comparison key instead of trusting a submitted duplicate value."""

        return company_name_key(self.name)
