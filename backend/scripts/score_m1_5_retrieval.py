"""Score M1.5 retrieval-only observations without implying answer-model quality."""

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from investment_research.modules.search.contracts import (
    M1_CONTEXT_CHARACTER_LIMIT,
    M1_CONTEXT_CHUNK_LIMIT,
)
from scripts.verify_m1_5_corpus import (
    DEFAULT_MANIFEST,
    REPOSITORY_ROOT,
    CorpusValidationError,
    EvaluationManifest,
    validate_corpus,
)


class RetrievalObservationError(Exception):
    """Reject invalid measurements without displaying source or question text."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class RetrievalConfiguration(_FrozenModel):
    lexical_config_version: str = Field(min_length=1)
    embedding_config_version: str = Field(min_length=1)
    dense_index_config_version: str = Field(min_length=1)
    selector_config_version: str = Field(min_length=1)
    embedding_adapter: Literal["deterministic_rules"]
    dense_adapter: Literal["deterministic_sqlite_cosine"]


class RetrievalCaseObservation(_FrozenModel):
    case_id: str = Field(min_length=1)
    selected_passage_ids: tuple[str, ...]
    selected_chunk_count: int = Field(ge=0, le=M1_CONTEXT_CHUNK_LIMIT)
    selected_character_count: int = Field(ge=0, le=M1_CONTEXT_CHARACTER_LIMIT)
    lexical_hit_count: int = Field(ge=0)
    qualified_dense_hit_count: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)


class RetrievalObservationSet(_FrozenModel):
    schema_version: Literal[1]
    corpus_id: str = Field(min_length=1)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration: RetrievalConfiguration
    run_mode: Literal["deterministic_retrieval_offline"]
    cases: tuple[RetrievalCaseObservation, ...] = Field(min_length=1)


class RetrievalCaseScore(_FrozenModel):
    case_id: str
    evidence_available_in_context: bool | None
    selected_chunk_count: int
    elapsed_ms: int


class RetrievalScoreSummary(_FrozenModel):
    corpus_id: str
    corpus_sha256: str
    configuration: RetrievalConfiguration
    run_mode: Literal["deterministic_retrieval_offline"]
    case_count: int
    answerable_case_count: int
    evidence_available_count: int
    median_retrieval_ms: float
    maximum_retrieval_ms: int
    cases: tuple[RetrievalCaseScore, ...]


def score_retrieval_observations(
    observations: RetrievalObservationSet,
    manifest: EvaluationManifest,
    manifest_sha256: str,
) -> RetrievalScoreSummary:
    """Score exact reviewed-evidence availability, not generated-answer correctness."""

    if manifest.review_status != "reviewed":
        raise RetrievalObservationError("the evaluation corpus has not been reviewed")
    if observations.corpus_id != manifest.id or observations.corpus_sha256 != manifest_sha256:
        raise RetrievalObservationError("observations do not match the reviewed corpus version")
    cases_by_id = {case.id: case for case in manifest.cases}
    observed_ids = [case.case_id for case in observations.cases]
    if len(observed_ids) != len(set(observed_ids)) or set(observed_ids) != cases_by_id.keys():
        raise RetrievalObservationError("observations must contain every case exactly once")

    passage_documents = {passage.id: passage.document_id for passage in manifest.passages}
    scores: list[RetrievalCaseScore] = []
    for observation in observations.cases:
        case = cases_by_id[observation.case_id]
        selected = observation.selected_passage_ids
        if len(selected) != len(set(selected)):
            raise RetrievalObservationError("selected passage IDs must be unique")
        if any(
            passage_documents.get(passage_id) not in case.scope_document_ids
            for passage_id in selected
        ):
            raise RetrievalObservationError(
                "selected evidence is unknown or outside the case scope"
            )
        if (observation.selected_chunk_count == 0) != (observation.selected_character_count == 0):
            raise RetrievalObservationError("selected chunk and character counts disagree")
        if selected and observation.selected_chunk_count == 0:
            raise RetrievalObservationError("a selected passage needs a selected chunk")
        matched = (
            None
            if case.expected_coverage == "INSUFFICIENT"
            else all(
                any(passage_id in selected for passage_id in group)
                for group in case.evidence_groups
            )
        )
        scores.append(
            RetrievalCaseScore(
                case_id=case.id,
                evidence_available_in_context=matched,
                selected_chunk_count=observation.selected_chunk_count,
                elapsed_ms=observation.elapsed_ms,
            )
        )

    return RetrievalScoreSummary(
        corpus_id=observations.corpus_id,
        corpus_sha256=observations.corpus_sha256,
        configuration=observations.configuration,
        run_mode=observations.run_mode,
        case_count=len(scores),
        answerable_case_count=sum(
            score.evidence_available_in_context is not None for score in scores
        ),
        evidence_available_count=sum(
            score.evidence_available_in_context is True for score in scores
        ),
        median_retrieval_ms=float(statistics.median(score.elapsed_ms for score in scores)),
        maximum_retrieval_ms=max(score.elapsed_ms for score in scores),
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
        observations = RetrievalObservationSet.model_validate_json(
            arguments.observations.read_bytes()
        )
        summary = score_retrieval_observations(
            observations,
            manifest,
            hashlib.sha256(manifest_bytes).hexdigest(),
        )
    except OSError, ValidationError, CorpusValidationError, RetrievalObservationError:
        print("M1.5 retrieval scoring failed; details withheld.", file=sys.stderr)
        return 1
    print(json.dumps(summary.model_dump(mode="json"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
