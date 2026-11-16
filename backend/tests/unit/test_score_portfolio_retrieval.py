"""Verify that offline coverage uses only the saved selection and reviewed query."""

import json
from typing import Literal

import pytest

from scripts.score_portfolio_retrieval import Case, Catalog, SnapshotError, Target, score


def sample_catalog() -> Catalog:
    cases = [
        Case(
            id=f"D{index:02}",
            kind="discovery",
            question="Find it",
            expected="Hit",
            discovery_scope="Keyword",
        )
        for index in range(1, 8)
    ]
    groups: tuple[tuple[Literal["answer", "abstention"], str, int], ...] = (
        ("answer", "A", 9),
        ("abstention", "U", 3),
    )
    for kind, prefix, count in groups:
        for index in range(1, count + 1):
            cases.append(
                Case(
                    id=f"{prefix}{index:02}",
                    kind=kind,
                    question="Original question",
                    expected="Expected fact",
                    document_scope=("target",),
                    context_targets=(
                        Target(id="amount", source="target", pages=(2,), markers=("3.5 billion",)),
                        Target(
                            id="terms", source="target", pages=(2,), markers=("364-day maturity",)
                        ),
                    ),
                    whole_archive_replay_question="Revised question"
                    if prefix == "A" and index == 6
                    else None,
                )
            )
    return Catalog(version=1, sources={"target": "Target", "other": "Other"}, cases=tuple(cases))


def snapshot_rows(catalog: Catalog) -> list[dict[str, object]]:
    return [
        {
            "case": case.id,
            "question": case.whole_archive_replay_question or case.question,
            "selector": "test-selector-v1",
            "app": [],
            "candidates": [],
        }
        for case in catalog.cases
        if case.kind != "discovery"
    ]


def to_jsonl(rows: list[dict[str, object]]) -> bytes:
    return "\n".join(map(json.dumps, rows)).encode()


def test_evidence_outside_selection_or_from_wrong_source_stays_missing() -> None:
    catalog = sample_catalog()
    rows = snapshot_rows(catalog)
    a05 = next(row for row in rows if row["case"] == "A05")
    a05["app"] = [4, 2, 1]
    a05["candidates"] = [
        {"id": 1, "title": "Target", "page": 2, "text": "Citi: 3.5 billion"},
        {"id": 2, "title": "Other", "page": 2, "text": "364-day maturity"},
        {"id": 3, "title": "Target", "page": 2, "text": "364-day maturity"},
        {"id": 4, "title": "Target", "page": 4, "text": "364-day maturity"},
    ]
    report = score(catalog, to_jsonl(rows))
    assert report["answer_cases_with_all_markers"] == 0
    cases = report["cases"]
    assert isinstance(cases, list)
    result = next(row for row in cases if row["case"] == "A05")
    assert result["evidence_marker_ranks"] == {"amount": [3], "terms": []}

    a05["app"] = [3, 1]
    assert score(catalog, to_jsonl(rows))["answer_cases_with_all_markers"] == 1


def test_rejects_mismatched_question_or_incomplete_snapshot() -> None:
    catalog = sample_catalog()
    rows = snapshot_rows(catalog)
    with pytest.raises(SnapshotError, match="all 12 research cases"):
        score(catalog, to_jsonl(rows[:-1]))
    a06 = next(row for row in rows if row["case"] == "A06")
    a06["question"] = "Original question"
    with pytest.raises(SnapshotError, match="A06: question differs"):
        score(catalog, to_jsonl(rows))
