"""Check short commands and frozen reports without Docker execution, credentials or providers."""

import hashlib
import json
import shutil
import socket
from pathlib import Path
from tomllib import loads
from typing import Any
from unittest.mock import Mock

import dotenv
import pytest

from scripts.evaluation_profiles import resolve_profile
from scripts.evaluation_report import render_comparison, render_run, write_report
from scripts.evaluation_run_artifacts import load_run
from scripts.portfolio_snapshots import OfflineEvaluationError, settings_for
from scripts.run_portfolio_evaluation import preview
from tests.unit.test_evaluate_portfolio import saved as saved
from tests.unit.test_evaluation_profiles import prepared as prepared
from tests.unit.test_evaluation_profiles import write_toml


@pytest.fixture
def bundle(saved: tuple[Path, Path, dict[str, Any]]) -> tuple[Path, Path]:
    profile, _, snapshot = saved
    values = loads(profile.read_text())
    values["mode"] = "retrieval_live"
    values["inputs"]["snapshot"] = ""
    values["budget"] = {"max_spend_usd": "0.08", "max_provider_calls": 100}
    write_toml(profile, values)
    resolved = resolve_profile(profile)
    snapshot["settings"] = settings_for(resolved).model_dump(mode="json")
    root = profile.parents[2]
    directory = root / "outputs/run"
    directory.mkdir()
    (directory / "profile.toml").write_bytes(resolved.profile_bytes)
    (directory / "catalog.json").write_bytes(resolved.catalog_bytes)
    (directory / "snapshot.json").write_text(json.dumps(snapshot))
    plan = preview(resolved)
    (directory / "resolved.json").write_text(json.dumps(plan))
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "plan": plan,
                "status": "completed",
                "observations": len(resolved.cases),
                "failed_observations": 0,
                "provider_calls": 8,
                "reserved_model_cost_micro_usd": 12,
                "profile_sha256": hashlib.sha256(resolved.profile_bytes).hexdigest(),
                "catalog_sha256": snapshot["catalog_sha256"],
                "code_sha256": snapshot["code_sha256"],
            }
        )
    )
    (directory / "results.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "case": case.id,
                    "status": "retrieved",
                    "elapsed_ms": index * 10,
                    "private_extra": "UNTRUSTED_RAW_PROVIDER_CONTENT",
                }
            )
            for index, case in enumerate(resolved.cases, 1)
        )
        + "\n"
    )
    return root, profile


def mutate(path: Path, function: Any) -> None:
    value = json.loads(path.read_text())
    function(value)
    path.write_text(json.dumps(value))


def test_report_uses_frozen_configuration_and_never_loads_env_or_calls_network(
    bundle: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, current_profile = bundle
    current_profile.write_text("invalid current configuration")
    (root / "config/rag.toml").write_text("invalid current configuration")
    forbidden = Mock(side_effect=AssertionError("must remain offline"))
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(dotenv, "dotenv_values", forbidden)
    monkeypatch.setattr(dotenv, "load_dotenv", forbidden)
    run = load_run("outputs/run")
    rendered = render_run(run)
    assert "3 / 3" in rendered and "USD 0.000012" in rendered
    assert "not billed cost" in rendered and "Manual review" in rendered
    assert "UNTRUSTED_RAW_PROVIDER_CONTENT" not in rendered
    assert run.scored is not None
    assert "3.5 billion" not in rendered
    assert run.definition.cases[0].question not in rendered
    for source in run.scored.snapshot.sources:
        assert str(source.document_id) not in rendered
    forbidden.assert_not_called()


def test_report_reads_saved_runs_without_the_new_decomposition_field(
    bundle: tuple[Path, Path],
) -> None:
    root, _ = bundle
    directory = root / "outputs/run"
    mutate(
        directory / "manifest.json",
        lambda item: item["plan"]["settings"].pop("query_decomposition_mode"),
    )
    mutate(
        directory / "snapshot.json", lambda item: item["settings"].pop("query_decomposition_mode")
    )

    report = render_run(load_run("outputs/run"))
    assert "<dt>Query decomposition</dt><dd>off</dd>" in report
    assert "<dt>Query planning attempts</dt><dd>Not captured</dd>" in report


def test_report_colors_only_retrieval_markers_and_preserves_unreviewed_outcomes(
    bundle: tuple[Path, Path],
) -> None:
    root, _ = bundle
    mutate(
        root / "outputs/run/snapshot.json", lambda value: value["selections"][1]["selected"].pop()
    )
    mutate(
        root / "outputs/run/snapshot.json", lambda value: value["selections"][2].update(selected=[])
    )
    rendered = render_run(load_run("outputs/run"))
    assert 'class="badge green"' in rendered
    assert 'class="badge orange"' in rendered
    assert 'class="badge red"' in rendered
    assert 'class="badge gray"' in rendered
    assert "do not establish correct answers" in rendered
    assert "Not captured by this runner version" in rendered


@pytest.mark.parametrize(
    "change", ["profile", "catalog", "code", "counts", "duplicate", "missing", "status"]
)
def test_inconsistent_bundles_fail_without_rendering_a_success_report(
    bundle: tuple[Path, Path], change: str
) -> None:
    root, _ = bundle
    directory = root / "outputs/run"
    if change in {"profile", "catalog"}:
        with (directory / ("profile.toml" if change == "profile" else "catalog.json")).open(
            "a"
        ) as stream:
            stream.write(" changed")
    elif change == "code":
        mutate(directory / "snapshot.json", lambda value: value.update(code_sha256="b" * 64))
    elif change == "counts":
        mutate(directory / "manifest.json", lambda value: value.update(observations=999))
    else:
        path = directory / "results.jsonl"
        lines = path.read_text().splitlines()
        if change == "duplicate":
            lines.append(lines[0])
        elif change == "missing":
            lines.pop()
        else:
            row = json.loads(lines[0])
            row["status"] = "STREAMED"
            lines[0] = json.dumps(row)
        path.write_text("\n".join(lines) + "\n")
    with pytest.raises((ValueError, OSError)):
        load_run("outputs/run")


def test_partial_run_keeps_missing_observations_gray(bundle: tuple[Path, Path]) -> None:
    root, _ = bundle
    directory = root / "outputs/run"
    path = directory / "results.jsonl"
    lines = path.read_text().splitlines()[:-1]
    path.write_text("\n".join(lines) + "\n")
    mutate(directory / "snapshot.json", lambda value: value["selections"].pop())
    mutate(
        directory / "manifest.json",
        lambda value: value.update(status="budget_exhausted", observations=3),
    )
    rendered = render_run(load_run("outputs/run"))
    assert "budget_exhausted" in rendered and "Not run" in rendered


def test_reports_escape_untrusted_labels_and_use_no_scripts_or_external_resources(
    bundle: tuple[Path, Path],
) -> None:
    root, _ = bundle
    directory = root / "outputs/run"
    catalog = json.loads((directory / "catalog.json").read_text())
    catalog["cases"][7]["context_targets"][0]["id"] = "<script>alert('untrusted')</script>"
    # A05 is selected in this bundle; put the label there too.
    next(case for case in catalog["cases"] if case["id"] == "A05")["context_targets"][0]["id"] = (
        "<script>alert('untrusted')</script>"
    )
    raw = json.dumps(catalog).encode()
    (directory / "catalog.json").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    mutate(directory / "manifest.json", lambda value: value.update(catalog_sha256=digest))
    mutate(directory / "snapshot.json", lambda value: value.update(catalog_sha256=digest))
    rendered = render_run(load_run("outputs/run"))
    assert "&lt;script&gt;" in rendered and "<script>" not in rendered
    assert "Content-Security-Policy" in rendered and "<iframe" not in rendered
    assert "<img" not in rendered and "<script src" not in rendered


def test_comparison_shows_lost_markers_ranks_and_durations(bundle: tuple[Path, Path]) -> None:
    root, _ = bundle
    shutil.copytree(root / "outputs/run", root / "outputs/candidate")
    mutate(
        root / "outputs/candidate/snapshot.json",
        lambda value: value["selections"][1]["selected"].pop(),
    )
    rendered = render_comparison(load_run("outputs/run"), load_run("outputs/candidate"))
    assert "lost:" in rendered and "+0 / -1" in rendered
    assert "20 → 20 ms" in rendered and "not recorded" in rendered
    assert "do not prove a causal" in rendered
    mutate(
        root / "outputs/candidate/snapshot.json",
        lambda value: value["sources"][0].update(original_sha256="c" * 64),
    )
    with pytest.raises(OfflineEvaluationError, match="corpus identities"):
        render_comparison(load_run("outputs/run"), load_run("outputs/candidate"))


def test_outputs_are_ignored_bounded_and_never_overwritten(bundle: tuple[Path, Path]) -> None:
    root, _ = bundle
    output = "outputs/reports/test.html"
    path = write_report(output, render_run(load_run("outputs/run")))
    assert path == root / output
    previous = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_report(output, "replacement")
    assert path.read_bytes() == previous
    with pytest.raises(ValueError):
        write_report("eval/public.html", "private")
    with pytest.raises(ValueError):
        load_run("outputs/../.env")
    target = root / "outputs/not-a-snapshot.toml"
    target.write_text("not a snapshot")
    (root / "outputs/run/snapshot.json").unlink()
    (root / "outputs/run/snapshot.json").symlink_to(target)
    with pytest.raises(ValueError):
        load_run("outputs/run")
