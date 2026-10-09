"""Regression tests for the versioned offline M1.5 evaluation catalog."""

import hashlib
import json
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.verify_m1_5_corpus import (
    DEFAULT_MANIFEST,
    REPOSITORY_ROOT,
    CorpusValidationError,
    main,
    validate_corpus,
)

CLOSEOUT_MANIFEST = REPOSITORY_ROOT / "fixtures/m1_5/evaluation-closeout-v1.json"
FROZEN_BASELINE_SHA256 = "ee526a2b708161e0c017b287529a62820978093e479fc78f2cea75a96a41d5f9"


def changed_manifest(
    tmp_path: Path,
    change: Callable[[dict[str, Any]], None],
    *,
    source: Path = DEFAULT_MANIFEST,
) -> Path:
    """Write one altered catalog while keeping the immutable fixture files unchanged."""

    data: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    change(data)
    path = tmp_path / "evaluation.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_catalog_matches_its_frozen_original_sources() -> None:
    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)

    assert manifest.id == "m1.5-rag-quality-v1"
    assert manifest.review_status == "reviewed"
    assert len(manifest.documents) == 3
    assert len(manifest.passages) == 6
    assert len(manifest.cases) == 6


def test_closeout_catalog_matches_originals_and_records_owner_review() -> None:
    manifest = validate_corpus(CLOSEOUT_MANIFEST, REPOSITORY_ROOT)

    assert manifest.id == "m1.5-rag-quality-closeout-v1"
    assert manifest.review_status == "reviewed"
    assert len(manifest.documents) == 3
    assert len(manifest.passages) == 7
    assert len(manifest.cases) == 12
    assert Counter(case.expected_coverage for case in manifest.cases) == {
        "COMPLETE": 7,
        "PARTIAL": 2,
        "CONFLICTING": 1,
        "INSUFFICIENT": 2,
    }


def test_closeout_catalog_preserves_the_frozen_six_case_baseline() -> None:
    baseline = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    closeout = validate_corpus(CLOSEOUT_MANIFEST, REPOSITORY_ROOT)

    assert hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest() == FROZEN_BASELINE_SHA256
    assert closeout.source_manifest == baseline.source_manifest
    assert closeout.documents == baseline.documents
    assert closeout.passages[:6] == baseline.passages
    assert closeout.cases[:6] == baseline.cases


def test_new_lead_time_target_does_not_relabel_the_old_rollout_case() -> None:
    manifest = validate_corpus(CLOSEOUT_MANIFEST, REPOSITORY_ROOT)
    cases = {case.id: case for case in manifest.cases}
    target = "passage:fy2026-supplier-lead-time-expectation"

    assert cases["question:fy2026-supplier-lead-time-uncertainty"].evidence_groups == ((target,),)
    assert cases["question:fy2026-rollout-constraint"].evidence_groups == (
        ("passage:fy2026-transformer-constraint",),
    )
    assert cases["question:fy2026-focused-constraint"].evidence_groups == (
        ("passage:fy2026-transformer-constraint",),
    )


def test_closeout_document_scope_does_not_grant_out_of_scope_budget_evidence() -> None:
    manifest = validate_corpus(CLOSEOUT_MANIFEST, REPOSITORY_ROOT)
    case = next(
        case for case in manifest.cases if case.id == "question:fy2026-pilot-budget-outside-scope"
    )
    budget_document = next(
        passage.document_id
        for passage in manifest.passages
        if passage.id == "passage:fy2026-european-pilot-budget-first"
    )

    assert budget_document not in case.scope_document_ids
    assert case.expected_coverage == "INSUFFICIENT"
    assert case.expected_answer is None
    assert case.evidence_groups == ()


def test_closeout_cli_checks_the_owner_reviewed_catalog(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["verify_m1_5_corpus", "--manifest", str(CLOSEOUT_MANIFEST)])

    assert main() == 0
    output = capsys.readouterr()
    assert "3 documents, 7 passages, 12 cases" in output.out
    assert "review_status=reviewed" in output.out


def test_closeout_cli_rejects_unreviewed_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unreviewed = changed_manifest(
        tmp_path,
        lambda data: data.update(review_status="pending_owner_review"),
        source=CLOSEOUT_MANIFEST,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_m1_5_corpus", "--manifest", str(unreviewed), "--require-reviewed"],
    )

    assert main() == 1
    output = capsys.readouterr()
    assert "awaits owner review" in output.err
    assert output.out == ""


def test_catalog_rejects_changed_fixture_digest(tmp_path: Path) -> None:
    def change(data: dict[str, Any]) -> None:
        data["documents"][2]["sha256"] = "0" * 64

    path = changed_manifest(tmp_path, change)

    with pytest.raises(CorpusValidationError, match="digest"):
        validate_corpus(path, REPOSITORY_ROOT)


def test_catalog_rejects_wrong_unicode_source_offset(tmp_path: Path) -> None:
    def change(data: dict[str, Any]) -> None:
        data["passages"][4]["source_anchor"]["start_character"] += 1

    path = changed_manifest(tmp_path, change)

    with pytest.raises(CorpusValidationError, match="source range"):
        validate_corpus(path, REPOSITORY_ROOT)


def test_catalog_rejects_evidence_outside_question_scope(tmp_path: Path) -> None:
    def change(data: dict[str, Any]) -> None:
        data["cases"][1]["evidence_groups"] = [["passage:fy2025-revenue-comparison"]]

    path = changed_manifest(tmp_path, change)

    with pytest.raises(CorpusValidationError, match="outside its scope"):
        validate_corpus(path, REPOSITORY_ROOT)


def test_catalog_rejects_conflict_without_two_sources(tmp_path: Path) -> None:
    def change(data: dict[str, Any]) -> None:
        data["cases"][3]["evidence_groups"] = [["passage:fy2026-transformer-constraint"]]

    path = changed_manifest(tmp_path, change)

    with pytest.raises(CorpusValidationError, match="distinct documents"):
        validate_corpus(path, REPOSITORY_ROOT)


def test_catalog_rejects_duplicate_case_id(tmp_path: Path) -> None:
    def change(data: dict[str, Any]) -> None:
        data["cases"].append(data["cases"][0])

    path = changed_manifest(tmp_path, change)

    with pytest.raises(CorpusValidationError, match="duplicate IDs"):
        validate_corpus(path, REPOSITORY_ROOT)


def test_catalog_rejects_fixture_path_escape(tmp_path: Path) -> None:
    def change(data: dict[str, Any]) -> None:
        data["documents"][2]["path"] = "fixtures/../../outside.txt"

    path = changed_manifest(tmp_path, change)

    with pytest.raises(CorpusValidationError, match="escapes the approved corpus"):
        validate_corpus(path, REPOSITORY_ROOT)


def test_catalog_rejects_path_inside_repository_but_outside_fixtures(tmp_path: Path) -> None:
    def change(data: dict[str, Any]) -> None:
        data["documents"][2]["path"] = "fixtures/../README.md"

    path = changed_manifest(tmp_path, change)

    with pytest.raises(CorpusValidationError, match="escapes the approved corpus"):
        validate_corpus(path, REPOSITORY_ROOT)


def test_unanswerable_case_cannot_require_a_citation(tmp_path: Path) -> None:
    def change(data: dict[str, Any]) -> None:
        data["cases"][2]["evidence_groups"] = [["passage:fy2026-transformer-constraint"]]

    path = changed_manifest(tmp_path, change)

    with pytest.raises(CorpusValidationError, match="insufficient case"):
        validate_corpus(path, REPOSITORY_ROOT)
