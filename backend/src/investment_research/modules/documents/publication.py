"""Shared UTC calendar-day bounds for catalog and retrieval queries."""

from datetime import UTC, date, datetime, time
from typing import Annotated, Self

from pydantic import BaseModel, BeforeValidator, model_validator


def parse_publication_day(value: object) -> date:
    """Accept a calendar day without coercing a timestamp or a number."""

    if type(value) is date:
        return value
    if isinstance(value, str):
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    raise ValueError("use a date in YYYY-MM-DD format")


PublicationDay = Annotated[date, BeforeValidator(parse_publication_day)]


class PublicationDateBounds(BaseModel):
    """Inclusive optional publication dates interpreted as UTC days."""

    published_from: PublicationDay | None = None
    published_to: PublicationDay | None = None

    @model_validator(mode="after")
    def require_ordered_publication_dates(self) -> Self:
        if (
            self.published_from is not None
            and self.published_to is not None
            and self.published_from > self.published_to
        ):
            raise ValueError("published_from must be on or before published_to")
        return self

    @property
    def publication_start(self) -> datetime | None:
        if self.published_from is None:
            return None
        return datetime.combine(self.published_from, time.min, UTC)

    @property
    def publication_end(self) -> datetime | None:
        if self.published_to is None:
            return None
        return datetime.combine(self.published_to, time.max, UTC)

    def matches_publication(self, published_at: datetime | None) -> bool:
        """Reapply the same bounds to a canonical source timestamp."""

        start, end = self.publication_start, self.publication_end
        if start is None and end is None:
            return True
        return (
            published_at is not None
            and (start is None or published_at >= start)
            and (end is None or published_at <= end)
        )
