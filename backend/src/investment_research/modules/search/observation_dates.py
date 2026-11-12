"""Recognize explicit observation dates without changing canonical source text."""

import re
from calendar import month_abbr, month_name
from datetime import date

_MONTHS = {
    name.casefold(): index
    for names in (month_name, month_abbr)
    for index, name in enumerate(names)
    if name
}
_MONTHS["sept"] = 9
_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))
_FULL_DATE = re.compile(
    rf"\b(?P<month>{_MONTH_PATTERN})\.?\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)
_OBSERVATION_PREFIX = re.compile(r"\b(?:as of|for)\s*$", re.IGNORECASE)


def _parse_date(match: re.Match[str]) -> date | None:
    try:
        return date(int(match["year"]), _MONTHS[match["month"].casefold()], int(match["day"]))
    except ValueError:
        return None


def requested_observation_date(question: str) -> date | None:
    """Require a single unambiguous English observation date in the original question."""
    dates = {
        parsed
        for match in _FULL_DATE.finditer(question)
        if _OBSERVATION_PREFIX.search(question[: match.start()])
        and (parsed := _parse_date(match)) is not None
    }
    return next(iter(dates)) if len(dates) == 1 else None


def contains_observation_date(text: str, expected: date) -> bool:
    """Compare full dates, including abbreviated month names and the year."""
    return any(_parse_date(match) == expected for match in _FULL_DATE.finditer(text))


def ends_with_observation_date(question: str) -> bool:
    """Recognize a single observation clause that ends at its requested date."""
    expected = requested_observation_date(question)
    return expected is not None and any(
        _parse_date(match) == expected and not question[match.end() :].strip(" ?.!")
        for match in _FULL_DATE.finditer(question)
    )


def preserve_observation_date(query: str, expected: date) -> str:
    """Keep a lexical refinement bound to the date supplied by the user."""
    date_text = f"{month_name[expected.month]} {expected.day} {expected.year}"
    if not _FULL_DATE.search(query):
        return f"{query} {date_text}"
    return _FULL_DATE.sub(
        lambda match: match.group() if _parse_date(match) == expected else date_text,
        query,
    )
