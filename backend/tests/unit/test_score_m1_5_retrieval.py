"""Unit tests for the retrieval-only M1.5 observation contract and scorer."""

import hashlib
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_research.modules.search.contracts import M1_SELECTOR_CONFIG_VERSION
from scripts.score_m1_5_retrieval import (
    RetrievalCaseObservation,
    RetrievalConfiguration,
    RetrievalObservationError,
    RetrievalObservationSet,
    RetrievalScoreSummary,
    main,
    score_retrieval_observations,
)
from scripts.verify_m1_5_corpus import DEFAULT_MANIFEST, REPOSITORY_ROOT, validate_corpus


def _controlled_observations() -> RetrievalObservationSet:
    """Create scorer input only; these values are not a measured M1 baseline."""

    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    return RetrievalObservationSet(
        schema_version=1,
        corpus_id=manifest.id,
        corpus_sha256=hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest(),
        configuration=RetrievalConfiguration(
            lexical_config_version="m1-english-lexical-v1",
            embedding_config_version="m1-synthetic-embedding-v1",
            dense_index_config_version="m1-synthetic-index-v1",
            selector_config_version=M1_SELECTOR_CONFIG_VERSION,
            embedding_adapter="deterministic_rules",
            dense_adapter="deterministic_sqlite_cosine",
        ),
        run_mode="deterministic_retrieval_offline",
        cases=tuple(
            RetrievalCaseObservation(
                case_id=case.id,
                selected_passage_ids=tuple(group[0] for group in case.evidence_groups),
                selected_chunk_count=1,
                selected_character_count=100,
                lexical_hit_count=1,
                qualified_dense_hit_count=1,
                elapsed_ms=10,
            )
            for case in manifest.cases
        ),
    )


def _score(observations: RetrievalObservationSet) -> RetrievalScoreSummary:
    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    return score_retrieval_observations(
        observations,
        manifest,
        hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest(),
    )


def test_scorer_reports_retrieval_only_counts_without_answer_metrics() -> None:
    summary = _score(_controlled_observations())

    assert summary.case_count == 6
    assert summary.answerable_case_count == 5
    assert summary.evidence_available_count == 5
    assert summary.median_retrieval_ms == 10
    assert summary.cases[2].evidence_available_in_context is None
    assert "coverage_match_count" not in summary.model_dump()
    assert "estimated_cost_micro_usd" not in summary.model_dump()


def test_missing_case_cannot_inflate_retrieval_result() -> None:
    observations = _controlled_observations()
    incomplete = observations.model_copy(update={"cases": observations.cases[:-1]})

    with pytest.raises(RetrievalObservationError, match="every case exactly once"):
        _score(incomplete)


def test_old_corpus_digest_is_rejected() -> None:
    observations = _controlled_observations()
    stale = observations.model_copy(update={"corpus_sha256": "0" * 64})

    with pytest.raises(RetrievalObservationError, match="corpus version"):
        _score(stale)


def test_conflict_requires_evidence_from_both_documents() -> None:
    observations = _controlled_observations()
    conflict = observations.cases[3].model_copy(
        update={"selected_passage_ids": ("passage:fy2026-transformer-constraint",)}
    )
    partial = observations.model_copy(
        update={"cases": (*observations.cases[:3], conflict, *observations.cases[4:])}
    )

    summary = _score(partial)

    assert summary.evidence_available_count == 4
    assert summary.cases[3].evidence_available_in_context is False


def test_out_of_scope_passage_is_rejected() -> None:
    observations = _controlled_observations()
    first = observations.cases[0].model_copy(
        update={"selected_passage_ids": ("passage:fy2026-transformer-constraint",)}
    )
    invalid = observations.model_copy(update={"cases": (first, *observations.cases[1:])})

    with pytest.raises(RetrievalObservationError, match="outside the case scope"):
        _score(invalid)


def test_passage_without_selected_chunk_is_rejected() -> None:
    observations = _controlled_observations()
    first = observations.cases[0].model_copy(
        update={"selected_chunk_count": 0, "selected_character_count": 0}
    )
    invalid = observations.model_copy(update={"cases": (first, *observations.cases[1:])})

    with pytest.raises(RetrievalObservationError, match="needs a selected chunk"):
        _score(invalid)


def test_schema_rejects_answer_claims_and_unbounded_context() -> None:
    payload = _controlled_observations().model_dump()
    payload["cases"][0]["answer_text"] = "unsupported"

    with pytest.raises(ValidationError):
        RetrievalObservationSet.model_validate(payload)

    del payload["cases"][0]["answer_text"]
    payload["cases"][0]["selected_chunk_count"] = 9
    with pytest.raises(ValidationError):
        RetrievalObservationSet.model_validate(payload)


def test_cli_emits_counts_without_questions_or_source_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "observations.json"
    path.write_text(_controlled_observations().model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["score_m1_5_retrieval", "--observations", str(path)])

    assert main() == 0

    output = capsys.readouterr().out
    summary = json.loads(output)
    assert summary["case_count"] == 6
    assert "Northstar" not in output
    assert "transformers" not in output
