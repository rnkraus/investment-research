"""Separate reported usage, unknown attempts and reservations without paid calls."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from investment_research.modules.operations.application import ProviderOperationCompletion
from investment_research.modules.operations.domain import (
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderOperationState,
)
from scripts.evaluation_report import cost_text, render_comparison, render_run, token_text
from scripts.evaluation_run_artifacts import RunArtifactError, load_run
from scripts.evaluation_usage import CaseUsage, UsageSummary, merge_usage
from scripts.portfolio_live_runtime import RunBudget, RunBudgetExceededError
from scripts.portfolio_snapshots import ArchiveMode
from tests.unit.test_evaluate_portfolio import saved as saved
from tests.unit.test_evaluation_profiles import prepared as prepared
from tests.unit.test_evaluation_report import bundle as bundle
from tests.unit.test_evaluation_report import mutate
from tests.unit.test_run_portfolio_evaluation import attempt


def completion(*, failed: bool = False) -> ProviderOperationCompletion:
    return ProviderOperationCompletion(
        state=ProviderOperationState.FAILED if failed else ProviderOperationState.SUCCEEDED,
        actual_cost_micro_usd=100 if failed else 20,
        input_units=None if failed else 20,
        output_units=None if failed else 0,
        completed_at=datetime(2026, 11, 20, tzinfo=UTC),
        duration_ms=1,
        safe_error_code=ProviderOperationErrorCode.UNAVAILABLE if failed else None,
    )


def test_retries_keep_unknown_usage_separate_from_known_cost_and_reservations() -> None:
    budget = RunBudget(300, 3)
    budget.begin_case("A06", 1, None)
    operation = attempt()
    budget.admit(operation)
    budget.completed(operation, completion(failed=True))
    budget.admit(operation)
    budget.completed(operation, completion())
    budget.admit(operation)  # Interrupted before any completion was finalized.
    with pytest.raises(RunBudgetExceededError):
        budget.admit(operation)
    usage = budget.usage()
    assert usage.total("attempts") == 3
    assert usage.total("reported_attempts") == 1
    assert usage.total("unknown_attempts") == 2
    assert usage.total("failed_attempts") == 1
    assert usage.total("reserved_micro_usd") == 300
    assert usage.total("estimated_usage_cost_micro_usd") == 20
    assert token_text(usage) == "20 / 0 (known part)"
    assert cost_text(usage) == "USD 0.000020 (known part)"
    assert budget.current_usage() == budget.case_usage()[0].usage == usage


def test_usage_distinguishes_case_mode_repetition_and_role() -> None:
    budget = RunBudget(1_000, 10)
    scopes: tuple[tuple[str, int, ArchiveMode | None], ...] = (
        ("A06", 1, None),
        ("A06", 2, None),
        ("D01", 1, "semantic"),
    )
    for case, repetition, mode in scopes:
        budget.begin_case(case, repetition, mode)
        operation = replace(attempt(), model=f"model-{repetition}")
        budget.admit(operation)
        budget.completed(operation, completion())
        answer = replace(operation, operation=ProviderOperationKind.ANSWER_GENERATION)
        budget.admit(answer)
        budget.completed(answer, completion())
    assert len(budget.case_usage()) == 3
    assert merge_usage([item.usage for item in budget.case_usage()]) == budget.usage()
    assert budget.current_usage().total("attempts") == 2
    assert {group.role for group in budget.usage().groups} == {
        "QUERY_EMBEDDING",
        "ANSWER_GENERATION",
    }
    with pytest.raises(ValueError, match="already started"):
        budget.begin_case("A06", 1, None)


def test_database_queries_are_excluded_without_fabricating_tokens_or_cost() -> None:
    budget = RunBudget(100, 2)
    budget.begin_case("D01", 1, "hybrid")
    provider = Mock()
    provider.retrieve.return_value = ()
    assert budget.wrap_dense(provider).retrieve(Mock(), Mock()) == ()
    provider.retrieve.side_effect = RuntimeError("private raw error")
    with pytest.raises(RuntimeError):
        budget.wrap_dense(provider).retrieve(Mock(), Mock())
    usage = budget.usage()
    assert usage.total("attempts") == usage.total("unmetered_attempts") == 2
    assert usage.total("failed_attempts") == 1
    assert usage.total("estimated_usage_cost_micro_usd") == 0
    assert token_text(usage) == "Not applicable (database)"
    assert cost_text(usage) == "Excluded database charges"
    assert "private raw error" not in json.dumps(budget.attempts)


def add_usage(root: Path) -> None:
    directory = root / "outputs/run"
    rows = [json.loads(line) for line in (directory / "results.jsonl").read_text().splitlines()]
    summaries: list[CaseUsage] = []
    for row in rows:
        budget = RunBudget(100, 1)
        budget.begin_case(row["case"], 1, None)
        operation = attempt()
        budget.admit(operation)
        budget.completed(operation, completion())
        row["provider_usage"] = budget.usage().model_dump(mode="json")
        summaries.extend(budget.case_usage())
    usage = merge_usage([item.usage for item in summaries])
    mutate(
        directory / "manifest.json",
        lambda value: value.update(
            provider_calls=usage.total("attempts"),
            reserved_model_cost_micro_usd=usage.total("reserved_micro_usd"),
            provider_usage=usage.model_dump(mode="json"),
            usage_by_case=[item.model_dump(mode="json") for item in summaries],
        ),
    )
    (directory / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_report_and_comparison_show_recorded_usage_without_repricing(
    bundle: tuple[Path, Path],
) -> None:
    root, _ = bundle
    add_usage(root)
    run = load_run("outputs/run")
    rendered = render_run(run)
    assert "Provider usage by role and model" in rendered
    assert "20 / 0" in rendered and "USD 0.000020" in rendered
    assert "USD 0.000100" in rendered  # Conservative reservation remains separate.
    assert "Interrupted" not in rendered and "Not run" not in rendered
    assert "QUERY_EMBEDDING" in rendered and "openai / test" in rendered
    assert "20 / 0" in render_comparison(run, run)


@pytest.mark.parametrize("change", ["run", "case", "journal", "missing", "duplicate", "legacy"])
def test_inconsistent_usage_cannot_be_reported_as_valid(
    bundle: tuple[Path, Path], change: str
) -> None:
    root, _ = bundle
    add_usage(root)
    directory = root / "outputs/run"
    if change == "journal":
        path = directory / "results.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]["provider_usage"] = UsageSummary().model_dump(mode="json")
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    else:

        def alter(value: dict[str, Any]) -> None:
            if change == "run":
                value["provider_calls"] += 1
            elif change == "case":
                value["usage_by_case"][0]["usage"]["groups"][0]["input_tokens"] += 1
            elif change == "missing":
                value["usage_by_case"].pop()
            elif change == "duplicate":
                value["usage_by_case"].append(value["usage_by_case"][0])
            else:
                value["provider_usage"] = None

        mutate(directory / "manifest.json", alter)
    with pytest.raises(RunArtifactError):
        load_run("outputs/run")


def test_interrupted_case_keeps_usage_without_becoming_a_success(bundle: tuple[Path, Path]) -> None:
    root, _ = bundle
    add_usage(root)
    directory = root / "outputs/run"
    rows = (directory / "results.jsonl").read_text().splitlines()
    (directory / "results.jsonl").write_text("\n".join(rows[:-1]) + "\n")
    mutate(directory / "snapshot.json", lambda value: value["selections"].pop())
    mutate(
        directory / "manifest.json",
        lambda value: value.update(
            status="budget_exhausted",
            observations=len(rows) - 1,
        ),
    )
    rendered = render_run(load_run("outputs/run"))
    assert rendered.count('class="badge orange">Interrupted') == 1
    assert "USD 0.000020" in rendered


def test_legacy_missing_usage_is_not_zero_and_empty_new_usage_is_zero() -> None:
    assert token_text(None) == cost_text(None) == "Not captured by this runner version"
    assert token_text(UsageSummary()) == "0 / 0"
    assert cost_text(UsageSummary()) == "USD 0.000000"
    budget = RunBudget(100, 1)
    budget.begin_case("A06", 1, None)
    budget.admit(attempt())
    assert token_text(budget.usage()) == cost_text(budget.usage()) == "Unknown"
