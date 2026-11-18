"""Test recorded retrieval scoring independently of providers and answer-quality judgments."""

import hashlib
import json
import socket
from pathlib import Path
from tomllib import loads
from typing import Any
from uuid import UUID

import dotenv
import pytest
from pydantic import ValidationError

from scripts import evaluate_portfolio as evaluator
from scripts.evaluation_profiles import resolve_profile
from scripts.portfolio_snapshots import OfflineEvaluationError, settings_for
from tests.unit.test_evaluation_profiles import prepared as prepared
from tests.unit.test_evaluation_profiles import write_toml


@pytest.fixture
def saved(prepared: tuple[Path, dict[str, Any]]) -> tuple[Path, Path, dict[str, Any]]:
    profile_path, values = prepared
    values["inputs"]["snapshot"] = "outputs/snapshot.json"
    write_toml(profile_path, values)
    resolved = resolve_profile(profile_path)
    snapshot_path = profile_path.parents[2] / "outputs/snapshot.json"
    snapshot_path.parent.mkdir()
    selections = []
    counter = 1_000
    for case in resolved.cases:
        selected = []
        for target in case.context_targets:
            counter += 1
            selected.append(
                {
                    "chunk_id": str(UUID(int=counter)),
                    "source": target.source,
                    "page": target.pages[0],
                    "text": " ".join(target.markers),
                }
            )
        selections.append(
            {
                "case": case.id,
                "question": case.question,
                "stage": "final_generation_context",
                "selected": selected,
            }
        )
    data = {
        "schema_version": 1,
        "catalog_sha256": hashlib.sha256(resolved.catalog_bytes).hexdigest(),
        "code_sha256": "a" * 64,
        "recorded_versions": {"answer_prompt": "synthetic-test-v1"},
        "settings": settings_for(resolved).model_dump(mode="json"),
        "sources": [
            {
                "alias": alias,
                "document_id": str(UUID(int=i)),
                "document_version_id": str(UUID(int=100 + i)),
                "title": title,
            }
            for i, (alias, title) in enumerate(resolved.catalog.sources.items(), 1)
        ],
        "selections": selections,
    }
    snapshot_path.write_text(json.dumps(data))
    return profile_path, snapshot_path, data


def test_scores_markers_without_network_credentials_or_answer_grading(
    saved: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path, _, _ = saved

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("offline scorer must not load credentials, connect, or write input files")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(dotenv, "load_dotenv", forbidden)
    monkeypatch.setattr(dotenv, "dotenv_values", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    result = evaluator.evaluate_saved(profile_path)
    assert result.report["provider_calls_made"] == 0
    assert result.report["summary"] == {
        "selected_cases": 4,
        "observations": 4,
        "answer_observations": 3,
        "answer_observations_with_all_markers": 3,
        "discovery_observations": 0,
        "abstention_observations": 1,
    }
    assert evaluator.evaluate_saved(profile_path).report == result.report
    rendered = json.dumps(result.report)
    assert "3.5 billion" not in rendered
    assert "What exact operating ARR" not in rendered


@pytest.mark.parametrize("change", ["question", "settings", "catalog", "missing", "scope"])
def test_rejects_changed_questions_settings_scope_or_missing_cases(
    saved: tuple[Path, Path, dict[str, Any]], change: str
) -> None:
    profile_path, snapshot_path, data = saved
    if change == "question":
        data["selections"][0]["question"] += " changed"
    elif change == "settings":
        data["settings"]["text_neighbors"] = False
    elif change == "catalog":
        data["catalog_sha256"] = "b" * 64
    elif change == "missing":
        data["selections"].pop()
    else:
        data["settings"]["scope"] = "catalog_documents"
    snapshot_path.write_text(json.dumps(data))
    with pytest.raises(OfflineEvaluationError):
        evaluator.evaluate_saved(profile_path)


@pytest.mark.parametrize("change", ["duplicate", "unknown_source", "oversize", "missing_limits"])
def test_rejects_invalid_recorded_identity_or_bounds(
    saved: tuple[Path, Path, dict[str, Any]], change: str
) -> None:
    profile_path, snapshot_path, data = saved
    if change == "duplicate":
        data["selections"][0]["selected"].append(data["selections"][0]["selected"][0])
    elif change == "unknown_source":
        data["selections"][0]["selected"][0]["source"] = "unknown"
    elif change == "oversize":
        data["selections"][0]["selected"][0]["text"] = "x" * 24_001
    else:
        del data["settings"]["rag"]["context"]["chunk_limit"]
    snapshot_path.write_text(json.dumps(data))
    with pytest.raises(ValidationError):
        evaluator.evaluate_saved(profile_path)


def test_comparison_reports_lost_evidence_and_passages_without_claiming_causality(
    saved: tuple[Path, Path, dict[str, Any]],
) -> None:
    profile_path, snapshot_path, data = saved
    baseline = evaluator.evaluate_saved(profile_path)
    del data["selections"][0]["selected"][0]
    snapshot_path.write_text(json.dumps(data))
    candidate = evaluator.evaluate_saved(profile_path)
    report = evaluator.compare_saved(candidate, baseline)
    first = report["comparisons"][0]  # type: ignore[index]
    assert first["lost_evidence_markers"] == ["facility-size-and-lender"]
    assert first["gained_evidence_markers"] == []
    assert len(first["removed_passages"]) == 1
    assert first["same_candidate_pool"] is None


@pytest.mark.parametrize("change", ["corpus", "stage", "shared_text"])
def test_comparison_rejects_unfair_or_inconsistent_snapshots(
    saved: tuple[Path, Path, dict[str, Any]], change: str
) -> None:
    profile_path, snapshot_path, data = saved
    baseline = evaluator.evaluate_saved(profile_path)
    if change == "corpus":
        data["sources"][0]["document_version_id"] = str(UUID(int=9_999))
    elif change == "stage":
        data["selections"][0]["stage"] = "selection_before_generation_preflight"
    else:
        data["selections"][0]["selected"][0]["text"] += " changed"
    snapshot_path.write_text(json.dumps(data))
    with pytest.raises(OfflineEvaluationError):
        evaluator.compare_saved(evaluator.evaluate_saved(profile_path), baseline)


def test_case_subset_and_cli_output_never_overwrite_existing_files(
    saved: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path, _, _ = saved
    result = evaluator.evaluate_saved(profile_path, case_ids=("U03",))
    assert len(result.rows) == 1 and result.rows[0].case == "U03"
    output = profile_path.parents[2] / "outputs/report.json"
    monkeypatch.setattr(
        "sys.argv", ["evaluate", "--profile", str(profile_path), "--output", "outputs/report.json"]
    )
    assert evaluator.main() == 0
    original = output.read_bytes()
    assert evaluator.main() == 1
    assert output.read_bytes() == original
    assert "3.5 billion" not in capsys.readouterr().out


def test_missing_repetition_does_not_duplicate_saved_results(
    saved: tuple[Path, Path, dict[str, Any]],
) -> None:
    profile_path, _, _ = saved
    values = loads(profile_path.read_text())
    values["repetitions"] = 2
    write_toml(profile_path, values)
    with pytest.raises(OfflineEvaluationError, match="repetition"):
        evaluator.evaluate_saved(profile_path)


def test_discovery_results_remain_manual_review_instead_of_an_invented_grade(
    saved: tuple[Path, Path, dict[str, Any]],
) -> None:
    profile_path, snapshot_path, data = saved
    values = loads(profile_path.read_text())
    values["cases"]["include"] = ["D01"]
    values["archive"]["modes"] = ["keyword"]
    write_toml(profile_path, values)
    data["selections"].append(
        {
            "case": "D01",
            "question": "NBIS",
            "archive_mode": "keyword",
            "stage": "archive_results",
            "selected": [],
        }
    )
    snapshot_path.write_text(json.dumps(data))
    result = evaluator.evaluate_saved(profile_path)
    row = result.report["observations"][0]  # type: ignore[index]
    assert row["all_markers_found"] is None
    assert row["manual_review_required"] is True


def test_document_scope_is_checked_against_saved_passages(
    saved: tuple[Path, Path, dict[str, Any]],
) -> None:
    profile_path, snapshot_path, data = saved
    values = loads(profile_path.read_text())
    values["scope"] = "catalog_documents"
    write_toml(profile_path, values)
    data["settings"]["scope"] = "catalog_documents"
    data["selections"][0]["selected"][0]["source"] = "I-Q1"
    snapshot_path.write_text(json.dumps(data))
    with pytest.raises(OfflineEvaluationError, match="scope"):
        evaluator.evaluate_saved(profile_path)


def test_live_mode_never_starts_a_provider_or_reads_a_snapshot(
    saved: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path, _, _ = saved
    values = loads(profile_path.read_text())
    values["mode"] = "rag_live"
    write_toml(profile_path, values)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("offline executor must reject live mode before reading a snapshot")

    monkeypatch.setattr(evaluator, "read_snapshot", forbidden)
    with pytest.raises(OfflineEvaluationError, match="offline_replay"):
        evaluator.evaluate_saved(profile_path)
