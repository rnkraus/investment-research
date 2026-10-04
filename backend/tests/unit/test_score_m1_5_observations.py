"""Unit tests for descriptive M1.5 observation scoring without providers."""

import hashlib
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_research.modules.search.contracts import M1_SELECTOR_CONFIG_VERSION
from scripts.score_m1_5_observations import (
    CaseObservation,
    ObservationSet,
    ObservationValidationError,
    ProviderUsage,
    RunConfiguration,
    ScoreSummary,
    main,
    score_observations,
)
from scripts.verify_m1_5_corpus import DEFAULT_MANIFEST, REPOSITORY_ROOT, validate_corpus


def make_complete_observations() -> ObservationSet:
    """Build controlled test observations, not a measured product baseline."""

    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    return ObservationSet(
        schema_version=1,
        corpus_id=manifest.id,
        corpus_sha256=hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest(),
        configuration=RunConfiguration(
            lexical_config_version="m1-english-lexical-v1",
            embedding_config_version="synthetic-embedding-v1",
            dense_index_config_version="synthetic-index-v1",
            selector_config_version=M1_SELECTOR_CONFIG_VERSION,
            answer_model="deterministic-answer",
            support_model="deterministic-support",
            answer_prompt_version="deterministic-answer-v1",
            support_prompt_version="deterministic-support-v1",
        ),
        run_mode="deterministic_offline",
        cases=tuple(
            CaseObservation(
                case_id=case.id,
                selected_passage_ids=tuple(group[0] for group in case.evidence_groups),
                cited_passage_ids=tuple(group[0] for group in case.evidence_groups),
                citation_count=len(case.evidence_groups),
                terminal_status="COMPLETED",
                coverage_status=case.expected_coverage,
                exact_citations_valid=True if case.evidence_groups else None,
                support_assessment=None,
                failed_safely=None,
                elapsed_ms=10,
                provider_usage=None,
            )
            for case in manifest.cases
        ),
    )


def score_controlled_observations(observations: ObservationSet) -> ScoreSummary:
    """Score against the committed review labels and exact catalog checksum."""

    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    return score_observations(
        observations,
        manifest,
        hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest(),
    )


def test_complete_controlled_observations_have_separate_metric_denominators() -> None:
    summary = score_controlled_observations(make_complete_observations())

    assert summary.case_count == 6
    assert (summary.retrieval_hit_count, summary.retrieval_case_count) == (5, 5)
    assert summary.coverage_match_count == 6
    assert summary.citation_match_count == 6
    assert (summary.exact_citation_valid_count, summary.exact_citation_checked_count) == (5, 5)
    assert summary.abstention_match_count == 1
    assert summary.conflict_match_count == 1
    assert summary.support_assessed_count == 0
    assert summary.median_elapsed_ms == 10
    assert summary.estimated_cost_micro_usd is None


def test_missing_case_is_rejected_instead_of_inflating_quality() -> None:
    observations = make_complete_observations()
    incomplete = observations.model_copy(update={"cases": observations.cases[:-1]})

    with pytest.raises(ObservationValidationError, match="every case exactly once"):
        score_controlled_observations(incomplete)


def test_old_corpus_digest_is_rejected() -> None:
    observations = make_complete_observations()
    stale = observations.model_copy(update={"corpus_sha256": "0" * 64})

    with pytest.raises(ObservationValidationError, match="corpus version"):
        score_controlled_observations(stale)


def test_citation_must_have_been_selected_into_context() -> None:
    observations = make_complete_observations()
    first = observations.cases[0].model_copy(update={"selected_passage_ids": ()})
    invalid = observations.model_copy(update={"cases": (first, *observations.cases[1:])})

    with pytest.raises(ObservationValidationError, match="not selected"):
        score_controlled_observations(invalid)


def test_conflict_needs_both_sources_for_retrieval_and_citation() -> None:
    observations = make_complete_observations()
    conflict = observations.cases[3].model_copy(
        update={
            "selected_passage_ids": ("passage:fy2026-transformer-constraint",),
            "cited_passage_ids": ("passage:fy2026-transformer-constraint",),
        }
    )
    partial = observations.model_copy(
        update={"cases": (*observations.cases[:3], conflict, *observations.cases[4:])}
    )

    summary = score_controlled_observations(partial)

    assert summary.retrieval_hit_count == 4
    assert summary.citation_match_count == 5
    assert summary.cases[3].retrieval_hit is False


def test_safe_failure_does_not_count_as_answer_success() -> None:
    observations = make_complete_observations()
    failed = observations.cases[0].model_copy(
        update={
            "cited_passage_ids": (),
            "citation_count": 0,
            "terminal_status": "FAILED",
            "coverage_status": None,
            "exact_citations_valid": None,
            "failed_safely": True,
        }
    )
    with_failure = observations.model_copy(update={"cases": (failed, *observations.cases[1:])})

    summary = score_controlled_observations(with_failure)

    assert summary.failed_safely_count == 1
    assert summary.coverage_match_count == 5
    assert summary.citation_match_count == 5


def test_unexpected_citation_is_scored_as_wrong_instead_of_rejecting_run() -> None:
    observations = make_complete_observations()
    first = observations.cases[0].model_copy(update={"citation_count": 2})
    with_extra_citation = observations.model_copy(
        update={"cases": (first, *observations.cases[1:])}
    )

    summary = score_controlled_observations(with_extra_citation)

    assert summary.cases[0].citation_match is False
    assert summary.citation_match_count == 5
    assert summary.exact_citation_valid_count == 5


def test_failed_unanswerable_case_does_not_count_as_correct_abstention() -> None:
    observations = make_complete_observations()
    failed = observations.cases[2].model_copy(
        update={
            "terminal_status": "FAILED",
            "coverage_status": None,
            "failed_safely": True,
        }
    )
    with_failure = observations.model_copy(
        update={"cases": (*observations.cases[:2], failed, *observations.cases[3:])}
    )

    summary = score_controlled_observations(with_failure)

    assert summary.failed_safely_count == 1
    assert summary.coverage_match_count == 5
    assert summary.citation_match_count == 5


def test_offline_run_cannot_claim_real_provider_usage() -> None:
    observations = make_complete_observations()
    first = observations.cases[0].model_copy(
        update={
            "provider_usage": ProviderUsage(
                input_tokens=10,
                output_tokens=5,
                estimated_cost_micro_usd=100,
            )
        }
    )
    invalid = observations.model_copy(update={"cases": (first, *observations.cases[1:])})

    with pytest.raises(ObservationValidationError, match="cannot claim provider usage"):
        score_controlled_observations(invalid)


def test_observation_schema_refuses_raw_prompt_or_source_content() -> None:
    observations = make_complete_observations()
    payload = observations.model_dump()
    payload["cases"][0]["raw_prompt"] = "not allowed"

    with pytest.raises(ValidationError):
        ObservationSet.model_validate(payload)


def test_cli_prints_counts_without_question_or_source_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "observations.json"
    path.write_text(make_complete_observations().model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["score_m1_5_observations", "--observations", str(path)])

    assert main() == 0

    output = capsys.readouterr().out
    summary = json.loads(output)
    assert summary["case_count"] == 6
    assert summary["configuration"]["selector_config_version"] == M1_SELECTOR_CONFIG_VERSION
    assert "Northstar" not in output
    assert "transformers" not in output
