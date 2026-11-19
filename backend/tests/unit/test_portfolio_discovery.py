"""Verify filtered discovery collection and mixed snapshots without live providers."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from tomllib import loads
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.bootstrap import ApiContainer
from investment_research.modules.companies.contracts import CompanyListPage, CompanySummary
from investment_research.modules.search.archive import ArchiveSearchInput, ArchiveSearchResult
from scripts import run_portfolio_evaluation as runner
from scripts.evaluate_portfolio import OfflineResult, compare_saved
from scripts.evaluation_profiles import (
    REPOSITORY_ROOT,
    DiscoveryFilters,
    EvaluationProfileError,
    ResolvedEvaluationProfile,
    resolve_profile,
)
from scripts.portfolio_discovery import collect_discovery, discovery_commands
from scripts.portfolio_live_runtime import LiveEvaluationError, RunBudget, sources_for
from scripts.portfolio_snapshots import (
    EvaluationSnapshot,
    OfflineEvaluationError,
    RecordedPassage,
    RecordedSelection,
    selected_rows,
)
from scripts.score_portfolio_retrieval import Case
from tests.unit.test_evaluation_profiles import prepared as prepared
from tests.unit.test_evaluation_profiles import write_toml
from tests.unit.test_run_portfolio_evaluation import stub_container

FULL_PROFILE = loads((REPOSITORY_ROOT / "eval/profiles/full-retrieval.example.toml").read_text())


@pytest.fixture
def full(prepared: tuple[Path, dict[str, Any]]) -> ResolvedEvaluationProfile:
    path, values = prepared
    values.update(
        mode="retrieval_live",
        archive=FULL_PROFILE["archive"],
        cases={"include": [], "exclude": []},
        budget={"max_spend_usd": "0.08", "max_provider_calls": 100},
    )
    write_toml(path, values)
    return resolve_profile(path)


def discovery_container(full: ResolvedEvaluationProfile) -> ApiContainer:
    # The shared stub's research context is unused by discovery.
    container, _ = stub_container(replace(full, cases=(full.catalog.cases[12],)))
    fake = cast(Mock, container)
    fake.company_service = Mock()
    fake.company_service.list_companies.return_value = CompanyListPage(
        items=(
            CompanySummary(
                company_id=UUID(int=901),
                name="IREN Limited",
                ticker="IREN",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            CompanySummary(
                company_id=UUID(int=902),
                name="Nebius Group",
                ticker="NBIS",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        )
    )
    fake.search_archive_service = Mock()
    fake.search_archive_service.search.return_value = ArchiveSearchResult(
        companies=(),
        documents=(),
        items=(),
        has_more=False,
    )
    return container


def test_structured_filters_preserve_every_reviewed_scope(full: ResolvedEvaluationProfile) -> None:
    container = discovery_container(full)
    commands = discovery_commands(container, container.identity_context_provider.resolve(), full)
    assert len(commands) == 21
    for mode in full.profile.archive.modes:
        assert commands[("D01", mode)].company_id is None
        assert commands[("D02", mode)].company_id == UUID(int=901)
        assert commands[("D03", mode)].company_id == UUID(int=901)
        assert commands[("D04", mode)].company_id == UUID(int=902)
        assert commands[("D05", mode)].published_to == date(2026, 7, 31)
        assert commands[("D05", mode)].company_id == UUID(int=901)
        assert commands[("D06", mode)].source_attribution == "@RealJimChanos"
        assert commands[("D06", mode)].source_attribution_kind == "handle"
        assert commands[("D07", mode)].company_id == UUID(int=901)
    assert runner.preview(full)["new_research_requests"] == 12
    assert runner.preview(full)["new_archive_searches"] == 21


@pytest.mark.parametrize("ambiguous", [False, True])
def test_missing_or_ambiguous_companies_fail_before_any_search(
    full: ResolvedEvaluationProfile, ambiguous: bool
) -> None:
    container = discovery_container(full)
    listing = cast(Mock, container.company_service.list_companies)
    first = listing.return_value.items[0]
    listing.return_value = CompanyListPage(
        items=((first, first.model_copy(update={"company_id": UUID(int=999)})) if ambiguous else ())
    )
    with pytest.raises(LiveEvaluationError, match="exactly one"):
        discovery_commands(container, container.identity_context_provider.resolve(), full)
    container.search_archive_service.search.assert_not_called()  # type: ignore[attr-defined]


def test_keyword_discovery_supports_zero_budget_and_never_generates(
    full: ResolvedEvaluationProfile,
) -> None:
    case = next(case for case in full.cases if case.id == "D01")
    resolved = replace(
        full,
        cases=(case,),
        profile=full.profile.model_copy(
            update={
                "mode": "rag_live",
                "archive": full.profile.archive.model_copy(update={"modes": ("keyword",)}),
                "budget": full.profile.budget.model_copy(
                    update={"max_spend_usd": "0.00", "max_provider_calls": 0}
                ),
            }
        ),
    )
    assert runner.preview(resolved)["generates_answers"] is False
    container = discovery_container(full)
    companies = container.company_service.list_companies.return_value.items  # type: ignore[attr-defined]
    container.search_archive_service.search.return_value = ArchiveSearchResult(  # type: ignore[attr-defined]
        companies=companies[1:],
        documents=(),
        items=(),
        has_more=False,
    )
    entries = container.list_documents_service.list_documents.return_value.items  # type: ignore[attr-defined]
    row, record = collect_discovery(
        container,
        sources_for(full, entries),
        case,
        1,
        ArchiveSearchInput(query=case.question, mode="keyword"),
    )
    assert row is not None and row.selected == ()
    assert record["status"] == "searched"
    assert record["result"]["companies"][0]["ticker"] == "NBIS"  # type: ignore[index]
    container.answer_research_request_service.answer.assert_not_called()  # type: ignore[attr-defined]
    container.retrieve_research_service.retrieve.assert_not_called()  # type: ignore[attr-defined]


def test_mixed_catalog_executes_every_case_mode_and_repetition_and_replays(
    full: ResolvedEvaluationProfile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = discovery_container(full)
    research_calls: list[str] = []

    def research(*args: Any) -> tuple[RecordedSelection, dict[str, object]]:
        case, repetition = args[3:5]
        research_calls.append(case.id)
        return RecordedSelection(
            case=case.id,
            question=case.question,
            repetition=repetition,
            stage="final_generation_context",
            selected=(),
        ), {"status": "retrieved"}

    monkeypatch.setattr(runner, "collect_case", research)
    profile = full.profile.model_copy(update={"repetitions": 2})
    resolved = replace(full, profile=profile)
    manifest = runner.collect(container, resolved, RunBudget(80_000, 100), tmp_path)
    assert manifest["status"] == "completed" and manifest["observations"] == 66
    assert len(research_calls) == 24
    assert container.search_archive_service.search.call_count == 42  # type: ignore[attr-defined]
    snapshot = EvaluationSnapshot.model_validate_json((tmp_path / "snapshot.json").read_text())
    assert len(selected_rows(resolved, snapshot)) == 66
    assert len(snapshot.settings.discovery_filters) == 7
    changed = full.profile.archive.model_copy(
        update={
            "case_filters": {
                **full.profile.archive.case_filters,
                "D05": DiscoveryFilters(company_ticker="IREN"),
            }
        }
    )
    with pytest.raises(OfflineEvaluationError, match="settings differ"):
        selected_rows(
            replace(resolved, profile=profile.model_copy(update={"archive": changed})), snapshot
        )


@pytest.mark.parametrize(
    "invalid",
    [
        {"company_ticker": "IREN", "company_name": "IREN Limited"},
        {"published_from": "2026-08-01", "published_to": "2026-07-31"},
        {"published_to": "2026-07-31T00:00:00Z"},
        {"source_attribution_kind": "handle"},
        {"workspace_id": "untrusted"},
    ],
)
def test_invalid_filter_tables_fail_closed(invalid: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DiscoveryFilters.model_validate(invalid)


def test_unknown_filter_case_is_rejected(prepared: tuple[Path, dict[str, Any]]) -> None:
    path, values = prepared
    values["archive"]["case_filters"] = {"D99": {}}
    write_toml(path, values)
    with pytest.raises(EvaluationProfileError, match="discovery cases"):
        resolve_profile(path)


def test_mixed_snapshot_retains_archive_preview_and_longer_research_context(
    full: ResolvedEvaluationProfile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = discovery_container(full)
    resolved = replace(
        full,
        cases=(full.cases[0], full.cases[7]),
        profile=full.profile.model_copy(
            update={"archive": full.profile.archive.model_copy(update={"modes": ("keyword",)})}
        ),
    )
    base = RecordedPassage(
        chunk_id=UUID(int=3001), source="N-INF", page=None, text="x" * 4000, text_is_truncated=True
    )

    def row(case: Case, stage: str) -> RecordedSelection:
        return RecordedSelection.model_validate(
            {
                "case": case.id,
                "question": case.question,
                "stage": stage,
                "archive_mode": "keyword" if stage == "archive_results" else None,
                "selected": (
                    base.model_dump()
                    if stage == "archive_results"
                    else base.model_copy(
                        update={"text": base.text + " tail", "text_is_truncated": False}
                    ).model_dump(),
                ),
            }
        )

    monkeypatch.setattr(
        runner,
        "collect_discovery",
        lambda *args: (row(args[2], "archive_results"), {"status": "searched"}),
    )
    monkeypatch.setattr(
        runner,
        "collect_case",
        lambda *args: (row(args[3], "final_generation_context"), {"status": "retrieved"}),
    )
    result = runner.collect(container, resolved, RunBudget(80_000, 100), tmp_path)
    assert result["status"] == "completed"
    snapshot = EvaluationSnapshot.model_validate_json((tmp_path / "snapshot.json").read_text())
    assert len(snapshot.selections) == 2
    baseline = OfflineResult(resolved, snapshot, tuple(reversed(snapshot.selections)), {})
    compare_saved(baseline, baseline)
    altered = snapshot.model_dump(mode="json")
    altered["selections"][1]["selected"][0]["text"] += " changed tail"
    candidate = EvaluationSnapshot.model_validate_json(json.dumps(altered))
    with pytest.raises(OfflineEvaluationError, match="conflicting recorded content"):
        compare_saved(OfflineResult(resolved, candidate, candidate.selections, {}), baseline)
    data = snapshot.model_dump(mode="json")
    data["selections"][1]["selected"][0]["text"] = "contradictory content"
    with pytest.raises(ValidationError, match="conflicting"):
        EvaluationSnapshot.model_validate_json(json.dumps(data))
