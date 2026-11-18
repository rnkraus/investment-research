"""Verify evaluation previews stay credential-free, bounded, and reproducible."""

import json
import socket
from copy import deepcopy
from pathlib import Path
from tomllib import loads
from typing import Any

import dotenv
import pytest

from scripts import evaluation_profiles as profiles
from scripts import plan_portfolio_evaluation as planner
from scripts.score_portfolio_retrieval import Catalog, SnapshotError, validate_catalog

ROOT = profiles.REPOSITORY_ROOT
TEMPLATE = (ROOT / "eval/profiles/standard.example.toml").read_text()
CATALOG = (ROOT / "eval/portfolio-cases-v1.json").read_bytes()
RAG = (ROOT / "config/rag.toml").read_bytes()


def write_toml(path: Path, values: dict[str, Any]) -> None:
    lines = []
    for key, value in values.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {json.dumps(value)}")
    for section, values_ in values.items():
        if isinstance(values_, dict):
            lines.append(f"[{section}]")
            lines.extend(f"{key} = {json.dumps(value)}" for key, value in values_.items())
    path.write_text("\n".join(lines))


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
    (tmp_path / "eval/profiles").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "eval/portfolio-cases-v1.json").write_bytes(CATALOG)
    (tmp_path / "config/rag.toml").write_bytes(RAG)
    monkeypatch.setattr(profiles, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(planner, "REPOSITORY_ROOT", tmp_path)
    return tmp_path / "eval/profiles/example.toml", loads(TEMPLATE)


def test_preview_inherits_shared_settings_and_uses_no_environment_network_or_writes(
    prepared: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, values = prepared
    write_toml(path, values)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("preview must not initialize providers, load credentials, or write files")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(dotenv, "dotenv_values", forbidden)
    monkeypatch.setattr(dotenv, "load_dotenv", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    result = planner.build_preview(path)
    assert result["execution_mode"] == "preflight_only"
    assert result["execution_available"] is False
    assert result["provider_calls_made"] == 0
    assert result["possible_live_provider_roles"] == []
    assert result["case_ids"] == ["A05", "A06", "A09", "U03"]
    assert result["planned_research_evaluations"] == 4
    assert result["effective_rag_settings"] == {
        "retrieval": {"lexical_candidate_limit": 20, "dense_candidate_limit": 20},
        "context": {"chunk_limit": 8, "character_limit": 24_000},
        "generation": {"input_overhead_limit": 18_000},
    }
    assert planner.build_preview(path) == result
    assert len(result["profile_sha256"]) == 64  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("repetitions", 11),
        ("repetitions", True),
        ("retrieval.chunk_limit", 9),
        ("retrieval.chunk_limit", "8"),
        ("retrieval.chunk_limit", True),
        ("retrieval.character_limit", 24_001),
        ("retrieval.lexical_candidate_limit", 0),
        ("retrieval.dense_candidate_limit", 21),
        ("generation.input_overhead_limit", 15_999),
        ("retrieval.unknown_switch", True),
        ("models.answer", "unapproved-model"),
        ("archive.modes", []),
        ("archive.modes", ["keyword", "keyword"]),
        ("cases.include", ["A05", "A05"]),
        ("cases.include", ["A99"]),
        ("cases.exclude", ["A05"]),
        ("budget.max_spend_usd", "3.00"),
        ("budget.max_spend_usd", 0.8),
        ("budget.max_spend_usd", "0.08"),
        ("budget.max_provider_calls", 1),
        ("inputs.snapshot", "../.secrets/key.json"),
        ("catalog", ".env"),
        ("catalog", "eval/../.env"),
        ("catalog", "/absolute/private.json"),
    ],
)
def test_invalid_settings_fail_without_exposing_values(
    prepared: tuple[Path, dict[str, Any]], field: str, invalid: object
) -> None:
    path, values = prepared
    section, separator, name = field.partition(".")
    if separator:
        values[section][name] = invalid
    else:
        values[section] = invalid
    write_toml(path, values)
    with pytest.raises(profiles.EvaluationProfileError):
        planner.build_preview(path)


def test_case_subset_retains_catalog_order_and_general_catalogs_are_supported(
    prepared: tuple[Path, dict[str, Any]],
) -> None:
    path, values = prepared
    values["cases"]["include"] = ["U03", "A05"]
    write_toml(path, values)
    assert planner.build_preview(path)["case_ids"] == ["A05", "U03"]
    values["cases"] = {"include": [], "exclude": ["A02"]}
    write_toml(path, values)
    assert len(planner.build_preview(path)["case_ids"]) == 18  # type: ignore[arg-type]
    catalog = json.loads(CATALOG)
    catalog["cases"] = [case for case in catalog["cases"] if case["id"] == "A01"]
    (path.parents[1] / "portfolio-cases-v1.json").write_text(json.dumps(catalog))
    values["cases"] = {"include": [], "exclude": []}
    write_toml(path, values)
    assert planner.build_preview(path)["case_ids"] == ["A01"]
    with pytest.raises(SnapshotError, match="19 unique"):
        validate_catalog(Catalog.model_validate(catalog))


def test_explicit_rag_overrides_do_not_modify_the_shared_config(
    prepared: tuple[Path, dict[str, Any]],
) -> None:
    path, values = prepared
    shared_path = path.parents[2] / "config/rag.toml"
    before = shared_path.read_bytes()
    values["retrieval"].update(chunk_limit=4, character_limit=8_000, lexical_candidate_limit=10)
    values["generation"]["input_overhead_limit"] = 16_000
    write_toml(path, values)
    result = planner.build_preview(path)
    assert result["effective_rag_settings"] == {
        "retrieval": {"lexical_candidate_limit": 10, "dense_candidate_limit": 20},
        "context": {"chunk_limit": 4, "character_limit": 8_000},
        "generation": {"input_overhead_limit": 16_000},
    }
    assert shared_path.read_bytes() == before


@pytest.mark.parametrize("mode", ["retrieval_live", "rag_live"])
@pytest.mark.parametrize("snapshot", ["", "outputs/saved-contexts.jsonl"])
def test_live_preview_reports_possible_roles_without_execution(
    prepared: tuple[Path, dict[str, Any]], mode: str, snapshot: str
) -> None:
    path, values = prepared
    values.update(mode=mode, repetitions=2)
    values["inputs"]["snapshot"] = snapshot
    values["budget"] = {"max_spend_usd": "0.08", "max_provider_calls": 20}
    write_toml(path, values)
    if snapshot and mode == "retrieval_live":
        with pytest.raises(profiles.EvaluationProfileError):
            planner.build_preview(path)
        return
    result = planner.build_preview(path)
    assert result["provider_calls_made"] == 0
    assert result["planned_research_evaluations"] == 8
    roles = set(result["possible_live_provider_roles"])  # type: ignore[call-overload]
    assert ("answer_generation" in roles) == (mode == "rag_live")
    assert ("query_embedding" in roles) == (not snapshot)


def test_keyword_only_discovery_needs_no_provider_budget_and_live_rag_does(
    prepared: tuple[Path, dict[str, Any]],
) -> None:
    path, values = prepared
    values["mode"] = "retrieval_live"
    values["cases"]["include"] = ["D01"]
    values["archive"]["modes"] = ["keyword"]
    write_toml(path, values)
    assert planner.build_preview(path)["possible_live_provider_roles"] == []
    values["cases"]["include"] = ["A05"]
    write_toml(path, values)
    with pytest.raises(profiles.EvaluationProfileError, match="positive"):
        planner.build_preview(path)


def test_incompatible_reranking_and_expansion_switches_are_rejected(
    prepared: tuple[Path, dict[str, Any]],
) -> None:
    path, values = prepared
    original = deepcopy(values)
    values["reranking"]["strategy"] = "pinecone"
    write_toml(path, values)
    with pytest.raises(profiles.EvaluationProfileError):
        planner.build_preview(path)
    values["retrieval"].update(text_neighbors=False, page_expansion=False, company_priority=False)
    write_toml(path, values)
    assert planner.build_preview(path)["requested_mode"] == "offline_replay"
    original["retrieval"].update(page_expansion=False, company_priority=False)
    original["reranking"]["strategy"] = "luna"
    write_toml(path, original)
    with pytest.raises(profiles.EvaluationProfileError):
        planner.build_preview(path)


def test_preview_rejects_secret_paths_before_reading_and_redacts_errors(
    prepared: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, values = prepared
    values["credential"] = "sensitive-value-must-not-be-reproduced"
    write_toml(path, values)
    monkeypatch.setattr("sys.argv", ["plan", "--profile", str(path)])
    assert planner.main() == 1
    assert values["credential"] not in capsys.readouterr().err

    def forbidden(*args: object, **kwargs: object) -> bytes:
        pytest.fail("invalid profile paths must not be opened")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    for forbidden_path in (
        path.parent / ".env",
        path.parent / ".env.toml",
        path.parents[2] / "private.toml",
    ):
        with pytest.raises(profiles.EvaluationProfileError):
            profiles.load_profile(forbidden_path)


def test_symlinked_catalog_cannot_escape_the_evaluation_directory(
    prepared: tuple[Path, dict[str, Any]],
) -> None:
    path, values = prepared
    outside = path.parents[2] / "outside.json"
    outside.write_bytes(CATALOG)
    (path.parents[1] / "escape.json").symlink_to(outside)
    values["catalog"] = "eval/escape.json"
    write_toml(path, values)
    with pytest.raises(profiles.EvaluationProfileError, match="escapes"):
        planner.build_preview(path)
