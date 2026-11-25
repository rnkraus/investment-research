"""Fill missing evidence for explicitly requested comparisons of two reporting quarters."""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from investment_research.modules.search.contracts import (
    LexicalSearchHit,
    SelectedRetrievalCandidate,
)
from investment_research.modules.search.domain import RetrievalOrigin
from investment_research.modules.search.observation_dates import requested_observation_date
from investment_research.modules.search.page_context import (
    STRUCTURED_CONTEXT_LIMIT,
    contains_reported_quantity,
    content_phrases,
    term_weights,
    weighted_overlap,
)

QUARTER_REPORT_LIMIT = 256
QUARTER_COMPARISON_DOCUMENT_LIMIT = 8
QUARTER_PASSAGE_LIMIT = 2048
_PROTECTED_SELECTIONS = 4
_QUARTER = re.compile(
    r"\b(?:q(?P<leading>[1-4])\b(?:[\s-]*(?P<fiscal>fy[\s-]*)?"
    r"(?P<year>20\d{2}|(?<=fy)\d{2})(?!\d))?|"
    r"(?P<trailing>[1-4])q(?P<compact>20\d{2}|\d{2})?\b)",
    re.IGNORECASE,
)
_WORD_QUARTER = re.compile(r"\b(first|second|third|fourth)[ -]quarter\b", re.IGNORECASE)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_FISCAL_YEAR = re.compile(r"\b(?:fy[\s-]*|fiscal(?:[\s-]+year)?[\s-]+)(20\d{2}|\d{2})\b", re.I)
_FISCAL_QUARTER = re.compile(r"\bfiscal(?:[ -]+year)?[ -]+q[1-4]\b", re.IGNORECASE)
_CALENDAR = re.compile(r"\bcalendar\b", re.IGNORECASE)
_OUTLOOK = re.compile(r"\b(?:outlook|guidance|forecast\w*|expect\w*)\b", re.IGNORECASE)
_CAPITAL_SPENDING = re.compile(r"\b(?:capital[\s-]+expenditures?|capex)\b", re.IGNORECASE)
_MEASURE_LABEL = re.compile(r"\b\d+(?:\.\d+)?\s*[-\u2013]\s*[a-z][a-z-]+\b", re.I)
_SEQUENTIAL = re.compile(
    r"\b(?:sequential(?:ly)?|(?:prior|previous|preceding) quarter|quarter[- ]over[- ]quarter)\b",
    re.IGNORECASE,
)
_AMOUNT_PATTERN = r"[$€£]\s*\d[\d.,]*\s*(?:billion|million)\b"
_AMOUNT = re.compile(_AMOUNT_PATTERN, re.IGNORECASE)
_AMOUNT_WITH_CHANGE = re.compile(
    rf"(?P<amount>{_AMOUNT_PATTERN})\s*[,;\u2013-]?\s*(?:up|down|increased|decreased)"
    r"\s+(?:by\s+)?\d+(?:\.\d+)?\s*%\s*(?:sequentially|"
    r"(?:from|versus|vs\.?|over)\s+(?:the\s+)?(?:prior|previous|preceding) quarter)",
    re.IGNORECASE,
)
_QUERY_NOISE = frozenset(
    {
        "chang",
        "compar",
        "expect",
        "includ",
        "measur",
        "move",
        "pres",
        "quarter",
        "releas",
        "report",
        "result",
        "revis",
        "sequenti",
        "year",
    }
)


@dataclass(frozen=True, slots=True)
class ReportingQuarter:
    """A reported period whose year is independent of the publication year."""

    number: int
    year: int
    fiscal: bool | None

    def matches(self, reported: ReportingQuarter) -> bool:
        return (self.number, self.year) == (reported.number, reported.year) and (
            self.fiscal is None or self.fiscal == reported.fiscal
        )


@dataclass(frozen=True, slots=True)
class QuarterPassage:
    """An eligible original passage with PostgreSQL's English search lexemes."""

    hit: LexicalSearchHit
    lexemes: frozenset[str]


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _year(value: str) -> int:
    year = int(value)
    return year + 2000 if year < 100 else year


def _periods(text: str) -> tuple[ReportingQuarter, ...]:
    text = _WORD_QUARTER.sub(lambda match: f"Q{_ORDINALS[match[1].lower()]}", text)
    fiscal_years = {_year(match[1]) for match in _FISCAL_YEAR.finditer(text)}
    years = {_year(match[1]) for match in _YEAR.finditer(text)} | fiscal_years
    default_year = next(iter(years)) if len(years) == 1 else None
    calendar = bool(_CALENDAR.search(text))
    has_fiscal_label = bool(_FISCAL_QUARTER.search(text))
    if calendar and (fiscal_years or has_fiscal_label):
        return ()

    periods: list[ReportingQuarter] = []
    for match in _QUARTER.finditer(text):
        year_text = match["year"] or match["compact"]
        year = _year(year_text) if year_text else default_year
        if year is None:
            # A repeated label can refer to one previously resolved quarter.
            prior = {
                period
                for period in periods
                if period.number == int(match["leading"] or match["trailing"])
            }
            if len(prior) != 1:
                return ()
            continue
        is_fiscal = (
            match["fiscal"] or year in fiscal_years or (has_fiscal_label and year == default_year)
        )
        fiscal = True if is_fiscal else False if calendar else None
        period = ReportingQuarter(int(match["leading"] or match["trailing"]), year, fiscal)
        if period not in periods:
            periods.append(period)
    return tuple(periods)


def comparison_quarters(question: str) -> tuple[ReportingQuarter, ...]:
    """Resolve exactly two labeled quarters and keep exact observations on their own path."""
    if requested_observation_date(question) is not None:
        return ()
    periods = _periods(question)
    return periods if len(periods) == 2 and periods[0].fiscal == periods[1].fiscal else ()


def report_quarter(title: str) -> ReportingQuarter | None:
    """Derive a period only from a title that identifies one quarter and year."""
    periods = _periods(title)
    return periods[0] if len(periods) == 1 else None


def _sequential_quarter(
    question: str, quarters: Sequence[ReportingQuarter]
) -> ReportingQuarter | None:
    cue = _SEQUENTIAL.search(question)
    if cue is None:
        return None
    clause = re.split(r"[.;!?]", question[cue.start() :], maxsplit=1)[0]
    labels = {int(match["leading"] or match["trailing"]) for match in _QUARTER.finditer(clause)}
    matches = [quarter for quarter in quarters if quarter.number in labels]
    if len(matches) == 1:
        return matches[0]
    first, last = sorted(quarters, key=lambda quarter: (quarter.year, quarter.number))
    if not labels and last.year * 4 + last.number - (first.year * 4 + first.number) == 1:
        return last
    return None


def _metric_amounts(question: str, hits: Sequence[SelectedRetrievalCandidate]) -> set[str]:
    phrases = [
        " ".join(pair) for pair in content_phrases(question) if {"revenue", "sales"} & set(pair)
    ]
    amounts: set[str] = set()
    for hit in hits:
        text = _normalized(hit.text)
        for phrase in phrases:
            for marker in re.finditer(rf"\b{re.escape(phrase)}\b", text):
                amount = _AMOUNT.search(text, marker.end(), marker.end() + 120)
                if amount is not None:
                    amounts.add(_normalized(amount[0]))
    return amounts


def add_quarter_context(
    question: str,
    quarters: Sequence[ReportingQuarter],
    reports: Mapping[UUID, ReportingQuarter],
    passages: Sequence[QuarterPassage],
    query_lexemes: frozenset[str],
    selected: tuple[SelectedRetrievalCandidate, ...],
    *,
    chunk_limit: int,
    character_limit: int,
) -> tuple[SelectedRetrievalCandidate, ...]:
    """Nominate one numeric passage per quarter and one requested sequential comparison."""
    if not passages:
        return selected
    query_terms = frozenset(
        term for term in query_lexemes - _QUERY_NOISE if term.isalpha() and len(term) > 1
    )
    weights = term_weights(query_terms, (item.lexemes for item in passages))
    phrases = content_phrases(question)
    measure_labels = set(_MEASURE_LABEL.findall(_normalized(question)))
    asks_capital_outlook = bool(_OUTLOOK.search(question) and _CAPITAL_SPENDING.search(question))

    def score(text: str, lexemes: frozenset[str]) -> float:
        normalized = _normalized(text)
        return (
            weighted_overlap(lexemes, weights)
            + 3 * len(phrases & content_phrases(normalized))
            + 8 * len(measure_labels.intersection(_MEASURE_LABEL.findall(normalized)))
            + 6
            * bool(
                asks_capital_outlook and _CAPITAL_SPENDING.search(text) and _OUTLOOK.search(text)
            )
        )

    by_id = {item.hit.chunk_id: item for item in passages}
    ranked = sorted(
        (item for item in passages if contains_reported_quantity(item.hit.text)),
        key=lambda item: (
            -score(item.hit.text, item.lexemes),
            len(item.hit.text),
            item.hit.chunk_id,
        ),
    )
    additions: list[LexicalSearchHit] = []
    for quarter in quarters:
        match = next((p for p in ranked if reports.get(p.hit.document_id) == quarter), None)
        if match is not None and score(match.hit.text, match.lexemes) > 0:
            additions.append(match.hit)

    change_quarter = _sequential_quarter(question, quarters)
    if change_quarter is not None:
        anchors = _metric_amounts(
            question, [hit for hit in selected if reports.get(hit.document_id) == change_quarter]
        )
        for passage in ranked:
            hit = passage.hit
            if reports.get(hit.document_id) == change_quarter and any(
                _normalized(match["amount"]) in anchors
                for match in _AMOUNT_WITH_CHANGE.finditer(_normalized(hit.text))
            ):
                additions.append(hit)
                break

    result = list(selected)
    protected = {hit.chunk_id for hit in additions} | {
        hit.chunk_id for hit in selected[:_PROTECTED_SELECTIONS]
    }
    for hit in additions:
        if any(item.chunk_id == hit.chunk_id for item in result):
            continue
        structured_full = sum(item.origin is RetrievalOrigin.STRUCTURED for item in result) >= (
            STRUCTURED_CONTEXT_LIMIT
        )
        removable = sorted(
            (
                (rank, item)
                for rank, item in enumerate(result)
                if item.chunk_id not in protected
                and (not structured_full or item.origin is RetrievalOrigin.STRUCTURED)
            ),
            key=lambda pair: (
                pair[1].document_id in reports,
                score(pair[1].text, by_id[pair[1].chunk_id].lexemes)
                if pair[1].chunk_id in by_id
                else 0.0,
                -pair[0],
            ),
        )
        removal_options: list[int | None] = [rank for rank, _ in removable]
        if len(result) < chunk_limit and not structured_full:
            removal_options.insert(0, None)
        for position in removal_options:
            kept = [item for rank, item in enumerate(result) if rank != position]
            if sum(len(item.text) for item in kept) + len(hit.text) <= character_limit:
                kept.insert(
                    _PROTECTED_SELECTIONS,
                    SelectedRetrievalCandidate(
                        **hit.model_dump(),
                        origin=RetrievalOrigin.STRUCTURED,
                        rank=1,
                        selection_rank=1,
                    ),
                )
                result = kept
                break
    return tuple(
        item.model_copy(update={"selection_rank": rank}) for rank, item in enumerate(result, 1)
    )
