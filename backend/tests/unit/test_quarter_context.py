"""Quarter inference and evidence selection stay conservative and provider-free."""

from uuid import UUID

import pytest

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.search.contracts import (
    LexicalSearchHit,
    SelectedRetrievalCandidate,
)
from investment_research.modules.search.domain import RetrievalOrigin
from investment_research.modules.search.quarter_context import (
    QuarterPassage,
    ReportingQuarter,
    add_quarter_context,
    comparison_quarters,
    report_quarter,
    revenue_forecast_actual_passages,
)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "Compare Q2 and Q3 FY2027.",
            (ReportingQuarter(2, 2027, True), ReportingQuarter(3, 2027, True)),
        ),
        (
            "Compare Q4 FY26 with Q1 FY27.",
            (ReportingQuarter(4, 2026, True), ReportingQuarter(1, 2027, True)),
        ),
        (
            "Compare Q2 with Q3 in calendar 2026.",
            (ReportingQuarter(2, 2026, False), ReportingQuarter(3, 2026, False)),
        ),
        ("What was revenue in Q2 and Q3?", ()),
        ("What was revenue in Q3 2026?", ()),
        ("Compare Q1, Q2 and Q3 2026.", ()),
        ("Compare Q2 and Q3 2026 as of February 13, 2027.", ()),
        ("Compare Q4 2026, Q1 2027 and Q2.", ()),
    ],
)
def test_comparison_quarters_requires_two_clear_periods(
    question: str, expected: tuple[ReportingQuarter, ...]
) -> None:
    assert comparison_quarters(question) == expected


def test_report_title_preserves_explicit_fiscal_year_and_rejects_a_comparison() -> None:
    fiscal = report_quarter("Example - FY2027-Q3 earnings release")
    assert fiscal == ReportingQuarter(3, 2027, True)
    assert fiscal is not None and not ReportingQuarter(3, 2027, False).matches(fiscal)
    assert report_quarter("Example announces Fiscal First Quarter 2027 results") == (
        ReportingQuarter(1, 2027, True)
    )
    assert report_quarter("Example announces 3Q26 financial results") == ReportingQuarter(
        3, 2026, None
    )
    assert report_quarter("Example Q2 and Q3 2026 comparison") is None


def passage(number: int, document: int, text: str, *terms: str) -> QuarterPassage:
    return QuarterPassage(
        hit=LexicalSearchHit(
            chunk_id=UUID(int=number),
            document_id=UUID(int=document),
            document_version_id=UUID(int=100 + document),
            structure_block_id=UUID(int=200 + number),
            source_locator_kind=SourceLocatorKind.PDF_PAGE,
            page_number=1,
            text=text,
            raw_score=1.0,
        ),
        lexemes=frozenset(terms),
    )


@pytest.mark.parametrize(
    ("actual_metric", "outlook_year", "header", "matching_pair"),
    [
        ("revenue", 2027, "YoYQ3\u201926Q3\u201927$ in millions", True),
        ("revenue", 2027, "In millions Q3'26 Q3'27", False),
        ("revenue", 2027, "YoYQ3'25Q3'26$ in millions", False),
        ("revenue", 2028, "YoYQ3'26Q3'27$ in millions", False),
        ("services revenue", 2027, "YoYQ3'26Q3'27$ in millions", False),
    ],
)
def test_forecast_and_actual_selection_requires_matching_metric_period_and_units(
    actual_metric: str, outlook_year: int, header: str, matching_pair: bool
) -> None:
    question = (
        "What full-year 2027 revenue range did Cedar guide to in Q2, "
        f"and what Q3 {actual_metric} did it subsequently report?"
    )
    outlook_quarter, actual_quarter = comparison_quarters(question)
    reports = {UUID(int=1): outlook_quarter, UUID(int=2): actual_quarter}
    outlook = passage(
        21, 1, f"Full-year {outlook_year} revenue guidance is $6.1 billion to $6.3 billion."
    )
    actual = passage(22, 2, f"{header} 17%$700.0 $819.0Revenue")
    later_outlook = passage(23, 2, "Full-year 2027 revenue guidance is $6.3-$6.5 billion.")
    other_company = passage(24, 3, "Q3'26 Q3'27 $ in millions $100 $200 Revenue")

    assert revenue_forecast_actual_passages(
        question,
        (outlook_quarter, actual_quarter),
        reports,
        (other_company, later_outlook, outlook, actual),
    ) == ((outlook.hit, actual.hit) if matching_pair else ())


def test_sequential_followup_links_the_comparison_to_the_retrieved_metric() -> None:
    question = (
        "Compare data center revenue in Q2 and Q3 FY2027. "
        "What sequential increase was reported in Q3?"
    )
    quarters = comparison_quarters(question)
    reports = {UUID(int=1): quarters[0], UUID(int=2): quarters[1]}
    q2 = passage(11, 1, "Data center revenue was $70 million.", "data", "center", "revenu")
    q3 = passage(12, 2, "Data center revenue was $84 million.", "data", "center", "revenu")
    incorrect = passage(
        13,
        2,
        "Total revenue was $100 million, up 25% from the previous quarter. "
        "Data center revenue was $84 million.",
        "data",
        "center",
        "revenu",
    )
    sequential = passage(
        14, 2, "Third-quarter revenue was $84 million, up 20% from the previous quarter.", "revenu"
    )
    fillers = tuple(passage(30 + index, 4, f"Industry update {index}.") for index in range(6))
    baseline = tuple(
        SelectedRetrievalCandidate(
            **item.hit.model_dump(), origin=RetrievalOrigin.LEXICAL, rank=rank, selection_rank=rank
        )
        for rank, item in enumerate((q2, q3, *fillers), 1)
    )

    result = add_quarter_context(
        question,
        quarters,
        reports,
        (q2, q3, incorrect, sequential),
        frozenset({"data", "center", "revenu"}),
        baseline,
        chunk_limit=8,
        character_limit=24_000,
    )

    assert len(result) == 8
    assert [item.chunk_id for item in result[:4]] == [item.chunk_id for item in baseline[:4]]
    assert [item.selection_rank for item in result] == list(range(1, 9))
    assert sequential.hit.chunk_id in {item.chunk_id for item in result}
