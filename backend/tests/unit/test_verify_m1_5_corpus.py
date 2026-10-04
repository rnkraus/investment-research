"""Regression tests for the versioned offline M1.5 evaluation catalog."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.verify_m1_5_corpus import (
    DEFAULT_MANIFEST,
    REPOSITORY_ROOT,
    CorpusValidationError,
    validate_corpus,
)


def changed_manifest(
    tmp_path: Path,
    change: Callable[[dict[str, Any]], None],
) -> Path:
    """Write one altered catalog while keeping the immutable fixture files unchanged."""

    data: dict[str, Any] = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
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
