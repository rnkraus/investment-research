"""Score complete M1.5 observations without calling retrieval or model providers."""

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scripts.verify_m1_5_corpus import (
    DEFAULT_MANIFEST,
    REPOSITORY_ROOT,
    CorpusValidationError,
    EvaluationManifest,
    validate_corpus,
)


class ObservationValidationError(Exception):
    """Report an invalid measurement without displaying questions or source text."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class ProviderUsage(_FrozenModel):
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    estimated_cost_micro_usd: int | None = Field(ge=0)


class SupportAssessment(_FrozenModel):
    method: Literal["human_review", "independent_model"]
    supported: bool


class RunConfiguration(_FrozenModel):
    lexical_config_version: str = Field(min_length=1)
    embedding_config_version: str = Field(min_length=1)
    dense_index_config_version: str = Field(min_length=1)
    selector_config_version: str = Field(min_length=1)
    answer_model: str = Field(min_length=1)
    support_model: str = Field(min_length=1)
    answer_prompt_version: str = Field(min_length=1)
    support_prompt_version: str = Field(min_length=1)


class CaseObservation(_FrozenModel):
    case_id: str = Field(min_length=1)
    selected_passage_ids: tuple[str, ...]
    cited_passage_ids: tuple[str, ...]
    citation_count: int = Field(ge=0)
    terminal_status: Literal["COMPLETED", "FAILED"]
    coverage_status: Literal["COMPLETE", "PARTIAL", "CONFLICTING", "INSUFFICIENT"] | None
    exact_citations_valid: bool | None
    support_assessment: SupportAssessment | None
    failed_safely: bool | None
    elapsed_ms: int = Field(ge=0)
    provider_usage: ProviderUsage | None


class ObservationSet(_FrozenModel):
    schema_version: Literal[1]
    corpus_id: str = Field(min_length=1)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration: RunConfiguration
    run_mode: Literal["deterministic_offline", "live_opt_in"]
    cases: tuple[CaseObservation, ...] = Field(min_length=1)


class CaseScore(_FrozenModel):
    case_id: str
    retrieval_hit: bool | None
    coverage_match: bool
    citation_match: bool
    exact_citation_valid: bool | None
    support_assessed: bool
    support_passed: bool | None
    failed_safely: bool | None
    elapsed_ms: int


class ScoreSummary(_FrozenModel):
    corpus_id: str
    corpus_sha256: str
    configuration: RunConfiguration
    run_mode: Literal["deterministic_offline", "live_opt_in"]
    case_count: int
    retrieval_hit_count: int
    retrieval_case_count: int
    coverage_match_count: int
    citation_match_count: int
    exact_citation_valid_count: int
    exact_citation_checked_count: int
    abstention_match_count: int
    conflict_match_count: int
    support_assessed_count: int
    support_passed_count: int
    failed_safely_count: int
    unsafe_failure_count: int
    median_elapsed_ms: float
    maximum_elapsed_ms: int
    provider_input_tokens: int | None
    provider_output_tokens: int | None
    estimated_cost_micro_usd: int | None
    cases: tuple[CaseScore, ...]


def _groups_present(groups: tuple[tuple[str, ...], ...], observed: set[str]) -> bool:
    return all(any(passage_id in observed for passage_id in group) for group in groups)


def _validate_observation_set(
    observations: ObservationSet,
    manifest: EvaluationManifest,
    manifest_sha256: str,
) -> None:
    if observations.corpus_id != manifest.id or observations.corpus_sha256 != manifest_sha256:
        raise ObservationValidationError("observations do not match the reviewed corpus version")
    expected_cases = {case.id: case for case in manifest.cases}
    observed_ids = [case.case_id for case in observations.cases]
    if len(observed_ids) != len(set(observed_ids)) or set(observed_ids) != expected_cases.keys():
        raise ObservationValidationError("observations must contain every case exactly once")
    passage_documents = {passage.id: passage.document_id for passage in manifest.passages}
    for observation in observations.cases:
        case = expected_cases[observation.case_id]
        selected = observation.selected_passage_ids
        cited = observation.cited_passage_ids
        if len(selected) != len(set(selected)) or len(cited) != len(set(cited)):
            raise ObservationValidationError("observation passage IDs must be unique")
        if any(
            passage_documents.get(passage_id) not in case.scope_document_ids
            for passage_id in (*selected, *cited)
        ):
            raise ObservationValidationError(
                "an observation refers to unknown or out-of-scope text"
            )
        if not set(cited) <= set(selected):
            raise ObservationValidationError("cited evidence was not selected into context")
        if observation.citation_count < len(cited):
            raise ObservationValidationError("citation count is smaller than matched evidence")
        if (observation.exact_citations_valid is None) != (observation.citation_count == 0):
            raise ObservationValidationError("citation validity must match citation presence")
        if observation.terminal_status == "FAILED":
            if (
                observation.coverage_status is not None
                or observation.citation_count
                or observation.support_assessment is not None
                or observation.failed_safely is None
            ):
                raise ObservationValidationError("a failed observation has inconsistent output")
        elif observation.coverage_status is None or observation.failed_safely is not None:
            raise ObservationValidationError("a completed observation needs a coverage status")
        if observations.run_mode == "deterministic_offline":
            if observation.provider_usage is not None:
                raise ObservationValidationError("offline observations cannot claim provider usage")
        elif observation.provider_usage is None:
            raise ObservationValidationError("live observations require provider usage")


def score_observations(
    observations: ObservationSet,
    manifest: EvaluationManifest,
    manifest_sha256: str,
) -> ScoreSummary:
    """Compute descriptive case counts without treating synthetic runs as live quality."""

    if manifest.review_status != "reviewed":
        raise ObservationValidationError("the evaluation corpus has not been reviewed")
    _validate_observation_set(observations, manifest, manifest_sha256)
    cases_by_id = {case.id: case for case in manifest.cases}
    scores: list[CaseScore] = []
    for observation in observations.cases:
        case = cases_by_id[observation.case_id]
        selected = set(observation.selected_passage_ids)
        cited = set(observation.cited_passage_ids)
        retrieval_hit = (
            None
            if case.expected_coverage == "INSUFFICIENT"
            else _groups_present(case.evidence_groups, selected)
        )
        citation_match = observation.terminal_status == "COMPLETED" and (
            observation.citation_count == 0
            if case.expected_coverage == "INSUFFICIENT"
            else bool(cited)
            and observation.citation_count == len(cited)
            and observation.exact_citations_valid is True
            and _groups_present(case.evidence_groups, cited)
        )
        scores.append(
            CaseScore(
                case_id=case.id,
                retrieval_hit=retrieval_hit,
                coverage_match=(
                    observation.terminal_status == "COMPLETED"
                    and observation.coverage_status == case.expected_coverage
                ),
                citation_match=citation_match,
                exact_citation_valid=observation.exact_citations_valid,
                support_assessed=observation.support_assessment is not None,
                support_passed=(
                    observation.support_assessment.supported
                    if observation.support_assessment is not None
                    else None
                ),
                failed_safely=observation.failed_safely,
                elapsed_ms=observation.elapsed_ms,
            )
        )
    usages = [case.provider_usage for case in observations.cases if case.provider_usage is not None]
    live = observations.run_mode == "live_opt_in"
    return ScoreSummary(
        corpus_id=observations.corpus_id,
        corpus_sha256=observations.corpus_sha256,
        configuration=observations.configuration,
        run_mode=observations.run_mode,
        case_count=len(scores),
        retrieval_hit_count=sum(score.retrieval_hit is True for score in scores),
        retrieval_case_count=sum(score.retrieval_hit is not None for score in scores),
        coverage_match_count=sum(score.coverage_match for score in scores),
        citation_match_count=sum(score.citation_match for score in scores),
        exact_citation_valid_count=sum(score.exact_citation_valid is True for score in scores),
        exact_citation_checked_count=sum(
            score.exact_citation_valid is not None for score in scores
        ),
        abstention_match_count=sum(
            score.coverage_match and score.citation_match
            for score in scores
            if cases_by_id[score.case_id].expected_coverage == "INSUFFICIENT"
        ),
        conflict_match_count=sum(
            score.coverage_match and score.citation_match
            for score in scores
            if cases_by_id[score.case_id].expected_coverage == "CONFLICTING"
        ),
        support_assessed_count=sum(score.support_assessed for score in scores),
        support_passed_count=sum(score.support_passed is True for score in scores),
        failed_safely_count=sum(score.failed_safely is True for score in scores),
        unsafe_failure_count=sum(score.failed_safely is False for score in scores),
        median_elapsed_ms=float(statistics.median(score.elapsed_ms for score in scores)),
        maximum_elapsed_ms=max(score.elapsed_ms for score in scores),
        provider_input_tokens=(
            sum(value for usage in usages if (value := usage.input_tokens) is not None)
            if live and all(usage.input_tokens is not None for usage in usages)
            else None
        ),
        provider_output_tokens=(
            sum(value for usage in usages if (value := usage.output_tokens) is not None)
            if live and all(usage.output_tokens is not None for usage in usages)
            else None
        ),
        estimated_cost_micro_usd=(
            sum(value for usage in usages if (value := usage.estimated_cost_micro_usd) is not None)
            if live and all(usage.estimated_cost_micro_usd is not None for usage in usages)
            else None
        ),
        cases=tuple(scores),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    arguments = parser.parse_args()
    try:
        manifest_bytes = arguments.manifest.read_bytes()
        manifest = validate_corpus(arguments.manifest, REPOSITORY_ROOT)
        observations = ObservationSet.model_validate_json(arguments.observations.read_bytes())
        summary = score_observations(
            observations,
            manifest,
            hashlib.sha256(manifest_bytes).hexdigest(),
        )
    except OSError, ValidationError, CorpusValidationError, ObservationValidationError:
        print("M1.5 observation scoring failed; details withheld.", file=sys.stderr)
        return 1
    print(json.dumps(summary.model_dump(mode="json"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
