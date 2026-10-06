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


def make_v2_observations() -> ObservationSet:
    observations = make_complete_observations()
    return observations.model_copy(
        update={
            "schema_version": 2,
            "cases": tuple(
                case.model_copy(
                    update={
                        "citation_passage_ids": tuple((item,) for item in case.cited_passage_ids)
                    }
                )
                for case in observations.cases
            ),
        }
    )


def test_complete_controlled_observations_have_separate_metric_denominators() -> None:
    summary = score_controlled_observations(make_complete_observations())

    assert summary.case_count == 6
    assert summary.scoring_version == "m1.5-observation-score-v1"
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


def test_v2_controlled_observations_record_the_new_policy_and_preserve_abstention() -> None:
    summary = score_controlled_observations(make_v2_observations())

    assert summary.scoring_version == "m1.5-observation-score-v2"
    assert summary.coverage_match_count == summary.citation_match_count == 6
    assert summary.abstention_match_count == summary.conflict_match_count == 1


@pytest.mark.parametrize("unmatched", [False, True])
@pytest.mark.parametrize("exact_valid", [False, True])
def test_v2_multiple_citations_require_every_citation_to_match_expected_evidence(
    unmatched: bool, exact_valid: bool
) -> None:
    observations = make_v2_observations()
    first = observations.cases[0]
    changed = first.model_copy(
        update={
            "citation_count": 2,
            "citation_passage_ids": (
                first.cited_passage_ids,
                () if unmatched else first.cited_passage_ids,
            ),
            "exact_citations_valid": exact_valid,
        }
    )
    summary = score_controlled_observations(
        observations.model_copy(update={"cases": (changed, *observations.cases[1:])})
    )

    assert summary.cases[0].citation_match is (exact_valid and not unmatched)
    assert summary.citation_match_count == (6 if exact_valid and not unmatched else 5)


def test_v2_extra_known_but_unexpected_passage_is_not_credited() -> None:
    observations = make_v2_observations()
    conflict = observations.cases[3]
    extra = "passage:fy2026-european-pilot-budget-first"
    assert conflict.citation_passage_ids is not None
    changed = conflict.model_copy(
        update={
            "selected_passage_ids": (*conflict.selected_passage_ids, extra),
            "cited_passage_ids": (*conflict.cited_passage_ids, extra),
            "citation_count": 3,
            "citation_passage_ids": (*conflict.citation_passage_ids, (extra,)),
        }
    )
    summary = score_controlled_observations(
        observations.model_copy(
            update={"cases": (*observations.cases[:3], changed, *observations.cases[4:])}
        )
    )

    assert summary.cases[3].citation_match is False
    assert summary.exact_citation_valid_count == 5


@pytest.mark.parametrize(
    "mappings",
    [
        None,
        (),
        ((),),
        (("passage:fy2025-revenue-comparison", "passage:fy2025-revenue-comparison"),),
    ],
)
def test_v2_incomplete_or_inconsistent_mappings_are_rejected(
    mappings: tuple[tuple[str, ...], ...] | None,
) -> None:
    observations = make_v2_observations()
    changed = observations.cases[0].model_copy(update={"citation_passage_ids": mappings})

    with pytest.raises(ObservationValidationError, match="mapping"):
        score_controlled_observations(
            observations.model_copy(update={"cases": (changed, *observations.cases[1:])})
        )


def test_legacy_observations_cannot_silently_adopt_the_new_mapping_policy() -> None:
    observations = make_v2_observations().model_copy(update={"schema_version": 1})

    with pytest.raises(ObservationValidationError, match="legacy"):
        score_controlled_observations(observations)


def test_historical_summary_without_a_policy_version_remains_v1() -> None:
    payload = score_controlled_observations(make_complete_observations()).model_dump()
    payload.pop("scoring_version")

    assert ScoreSummary.model_validate(payload).scoring_version == "m1.5-observation-score-v1"


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


def test_unknown_live_usage_does_not_become_a_zero_cost_total() -> None:
    observations = make_complete_observations()
    cases = tuple(
        case.model_copy(
            update={
                "provider_usage": ProviderUsage(
                    input_tokens=None if index == 0 else 10,
                    output_tokens=None if index == 0 else 5,
                    estimated_cost_micro_usd=None if index == 0 else 100,
                )
            }
        )
        for index, case in enumerate(observations.cases)
    )
    live = observations.model_copy(update={"run_mode": "live_opt_in", "cases": cases})

    summary = score_controlled_observations(live)

    assert summary.provider_input_tokens is None
    assert summary.provider_output_tokens is None
    assert summary.estimated_cost_micro_usd is None
    assert summary.case_count == 6


def test_reported_live_usage_is_summed_without_inventing_support_review() -> None:
    observations = make_complete_observations()
    cases = tuple(
        case.model_copy(
            update={
                "provider_usage": ProviderUsage(
                    input_tokens=10, output_tokens=5, estimated_cost_micro_usd=100
                )
            }
        )
        for case in observations.cases
    )

    summary = score_controlled_observations(
        observations.model_copy(update={"run_mode": "live_opt_in", "cases": cases})
    )

    assert summary.provider_input_tokens == 60
    assert summary.provider_output_tokens == 30
    assert summary.estimated_cost_micro_usd == 600
    assert summary.support_assessed_count == 0


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
