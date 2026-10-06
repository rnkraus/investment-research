"""Compare M1 and RRF on identical qualified offline lanes without answer-quality claims."""

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from time import perf_counter_ns
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from investment_research.modules.processing.domain import StructureBlock
from investment_research.modules.search.contracts import (
    M1_SELECTOR_CONFIG_VERSION,
    LexicalSearchHit,
    M1SelectionProfile,
    QualifiedDenseHit,
    SelectedRetrievalCandidate,
)
from investment_research.modules.search.fusion import select_rrf_context
from investment_research.modules.search.fusion_contracts import (
    RRF_CONFIG_VERSION,
    RrfSelectionProfile,
)
from investment_research.modules.search.selection import select_m1_context
from scripts.map_m1_5_passages import map_selected_passages
from scripts.score_m1_5_retrieval import (
    RetrievalCaseObservation,
    RetrievalConfiguration,
    RetrievalObservationError,
    RetrievalObservationSet,
    score_retrieval_observations,
)
from scripts.verify_m1_5_corpus import (
    DEFAULT_MANIFEST,
    REPOSITORY_ROOT,
    Case,
    CorpusValidationError,
    EvaluationManifest,
    validate_corpus,
)

type EvidenceRank = Annotated[int, Field(ge=1, le=8)] | None


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class ContextObservation(RetrievalCaseObservation):
    """Retain selection order and each required evidence group's first context position."""

    selected_chunk_ids: tuple[UUID, ...] = Field(max_length=8)
    evidence_group_first_ranks: tuple[EvidenceRank, ...]
    selector_elapsed_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def require_coherent_order(self) -> ContextObservation:
        if len(self.selected_chunk_ids) != self.selected_chunk_count or len(
            set(self.selected_chunk_ids)
        ) != len(self.selected_chunk_ids):
            raise ValueError("selected chunk identities must be unique and match their count")
        if any(
            rank is not None and rank > self.selected_chunk_count
            for rank in self.evidence_group_first_ranks
        ):
            raise ValueError("evidence rank exceeds the selected context")
        return self


class PairedCaseObservation(_FrozenModel):
    """One shared qualified-lane snapshot, with two independent selector measurements."""

    qualified_lanes_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    shared_retrieval_elapsed_ns: int = Field(ge=0)
    m1: ContextObservation
    rrf: ContextObservation

    @model_validator(mode="after")
    def require_paired_inputs(self) -> PairedCaseObservation:
        if (self.m1.case_id, self.m1.lexical_hit_count, self.m1.qualified_dense_hit_count) != (
            self.rrf.case_id,
            self.rrf.lexical_hit_count,
            self.rrf.qualified_dense_hit_count,
        ):
            raise ValueError("paired selectors must share the case and lane counts")
        if max(self.m1.lexical_hit_count, self.m1.qualified_dense_hit_count) > 20:
            raise ValueError("paired lanes must remain within twenty hits")
        for observation in (self.m1, self.rrf):
            if (
                observation.elapsed_ms
                != (self.shared_retrieval_elapsed_ns + observation.selector_elapsed_ns) // 1_000_000
            ):
                raise ValueError("elapsed time must include the shared retrieval and selector only")
        return self


class RetrievalComparison(_FrozenModel):
    schema_version: Literal[1] = 1
    comparison_version: Literal["m1.5-paired-retrieval-v1"] = "m1.5-paired-retrieval-v1"
    run_mode: Literal["paired_deterministic_retrieval_offline"] = (
        "paired_deterministic_retrieval_offline"
    )
    corpus_id: str = Field(min_length=1)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration: RetrievalConfiguration
    parser_version: str = Field(min_length=1)
    parser_config_version: str = Field(min_length=1)
    chunker_version: str = Field(min_length=1)
    chunking_config_version: str = Field(min_length=1)
    test_identity_config_version: Literal["m1.5-paired-identities-v1"] = "m1.5-paired-identities-v1"
    rrf_selector_config_version: Literal["m1-5-rrf-k60-v1"] = RRF_CONFIG_VERSION
    context_chunk_limit: Literal[8] = 8
    context_character_limit: Literal[24_000] = 24_000
    cases: tuple[PairedCaseObservation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_m1_baseline(self) -> RetrievalComparison:
        if self.configuration.selector_config_version != M1_SELECTOR_CONFIG_VERSION:
            raise ValueError("the comparison requires the frozen M1 selector")
        return self


def observe_selector_pair(
    *,
    case: Case,
    manifest: EvaluationManifest,
    lexical_hits: Sequence[LexicalSearchHit],
    dense_hits: Sequence[QualifiedDenseHit],
    blocks_by_id: Mapping[UUID, StructureBlock],
    catalog_document_ids: Mapping[UUID, str],
    shared_retrieval_elapsed_ns: int,
) -> PairedCaseObservation:
    """Run both selectors over the same immutable canonical lists and default budgets."""

    lexical = tuple(lexical_hits)
    dense = tuple(dense_hits)
    start = perf_counter_ns()
    m1 = select_m1_context(lexical_hits=lexical, dense_hits=dense, profile=M1SelectionProfile())
    m1_elapsed = perf_counter_ns() - start
    start = perf_counter_ns()
    rrf = select_rrf_context(lexical_hits=lexical, dense_hits=dense, profile=RrfSelectionProfile())
    rrf_elapsed = perf_counter_ns() - start
    rrf_selected = tuple(
        SelectedRetrievalCandidate(
            **item.candidate.hit.model_dump(),
            origin=item.candidate.contributions[0].origin,
            rank=item.candidate.contributions[0].rank,
            selection_rank=item.selection_rank,
        )
        for item in rrf.candidates
    )

    def observe(
        selected: tuple[SelectedRetrievalCandidate, ...],
        selector_elapsed_ns: int,
    ) -> ContextObservation:
        mapped = tuple(
            map_selected_passages(
                manifest,
                (item,),
                blocks_by_id=blocks_by_id,
                catalog_document_ids=catalog_document_ids,
            )
            for item in selected
        )
        present = {passage_id for ids in mapped for passage_id in ids}
        return ContextObservation(
            case_id=case.id,
            selected_passage_ids=tuple(p.id for p in manifest.passages if p.id in present),
            selected_chunk_count=len(selected),
            selected_chunk_ids=tuple(c.chunk_id for c in selected),
            selected_character_count=sum(len(c.text) for c in selected),
            lexical_hit_count=len(lexical),
            qualified_dense_hit_count=len(dense),
            evidence_group_first_ranks=tuple(
                next(
                    (
                        rank
                        for rank, ids in enumerate(mapped, start=1)
                        if set(group).intersection(ids)
                    ),
                    None,
                )
                for group in case.evidence_groups
            ),
            selector_elapsed_ns=selector_elapsed_ns,
            elapsed_ms=(shared_retrieval_elapsed_ns + selector_elapsed_ns) // 1_000_000,
        )

    # Hash stable logical source identities, not transient database/workspace UUIDs or raw text.
    snapshot = {}
    for name, hits in (("lexical", lexical), ("dense", dense)):
        snapshot[name] = [
            {
                "chunk_id": str(hit.chunk_id),
                "document_id": catalog_document_ids[hit.document_id],
                "source_locator_kind": hit.source_locator_kind.value,
                "page_number": hit.page_number,
                "block_start_character": blocks_by_id[hit.structure_block_id].start_character,
                "text_sha256": hashlib.sha256(hit.text.encode("utf-8")).hexdigest(),
                "raw_score": hit.raw_score,
            }
            for hit in hits
        ]
    return PairedCaseObservation(
        qualified_lanes_sha256=hashlib.sha256(
            json.dumps(
                snapshot,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        shared_retrieval_elapsed_ns=shared_retrieval_elapsed_ns,
        m1=observe(m1.candidates, m1_elapsed),
        rrf=observe(rrf_selected, rrf_elapsed),
    )


def score_comparison(
    comparison: RetrievalComparison,
    manifest: EvaluationManifest,
    manifest_sha256: str,
) -> dict[str, object]:
    """Reuse the reviewed availability metric; report rank changes without choosing a default."""

    summaries = {}
    for policy in ("m1", "rrf"):
        observations = tuple(getattr(pair, policy) for pair in comparison.cases)
        configuration = comparison.configuration.model_copy(
            update={
                "selector_config_version": (
                    M1_SELECTOR_CONFIG_VERSION if policy == "m1" else RRF_CONFIG_VERSION
                ),
            }
        )
        observed = RetrievalObservationSet(
            schema_version=1,
            corpus_id=comparison.corpus_id,
            corpus_sha256=comparison.corpus_sha256,
            configuration=configuration,
            run_mode="deterministic_retrieval_offline",
            cases=tuple(
                RetrievalCaseObservation.model_validate(
                    item.model_dump(
                        exclude={
                            "selected_chunk_ids",
                            "evidence_group_first_ranks",
                            "selector_elapsed_ns",
                        }
                    )
                )
                for item in observations
            ),
        )
        summaries[policy] = score_retrieval_observations(observed, manifest, manifest_sha256)

    cases_by_id = {case.id: case for case in manifest.cases}
    for pair in comparison.cases:
        groups = cases_by_id[pair.m1.case_id].evidence_groups
        for observation in (pair.m1, pair.rrf):
            if len(observation.evidence_group_first_ranks) != len(groups) or any(
                (rank is not None)
                != bool(set(group).intersection(observation.selected_passage_ids))
                for group, rank in zip(groups, observation.evidence_group_first_ranks, strict=True)
            ):
                raise RetrievalObservationError(
                    "evidence ranks must match the reviewed group mappings"
                )
    differences = [
        {
            "case_id": pair.m1.case_id,
            "qualified_lanes_sha256": pair.qualified_lanes_sha256,
            "shared_retrieval_elapsed_ns": pair.shared_retrieval_elapsed_ns,
            "selection_changed": pair.m1.selected_chunk_ids != pair.rrf.selected_chunk_ids,
            "selected_set_changed": set(pair.m1.selected_chunk_ids)
            != set(pair.rrf.selected_chunk_ids),
            "m1_evidence_group_first_ranks": pair.m1.evidence_group_first_ranks,
            "rrf_evidence_group_first_ranks": pair.rrf.evidence_group_first_ranks,
            "m1_selector_elapsed_ns": pair.m1.selector_elapsed_ns,
            "rrf_selector_elapsed_ns": pair.rrf.selector_elapsed_ns,
        }
        for pair in comparison.cases
    ]
    return {
        "comparison_version": comparison.comparison_version,
        "run_mode": comparison.run_mode,
        "parser_version": comparison.parser_version,
        "parser_config_version": comparison.parser_config_version,
        "chunker_version": comparison.chunker_version,
        "chunking_config_version": comparison.chunking_config_version,
        "test_identity_config_version": comparison.test_identity_config_version,
        "m1": summaries["m1"].model_dump(mode="json"),
        "rrf": summaries["rrf"].model_dump(mode="json"),
        "cases": differences,
        "selection_changed_count": sum(bool(item["selection_changed"]) for item in differences),
        "selected_set_changed_count": sum(
            bool(item["selected_set_changed"]) for item in differences
        ),
        "default_policy_changed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    arguments = parser.parse_args()
    try:
        manifest = validate_corpus(arguments.manifest, REPOSITORY_ROOT)
        comparison = RetrievalComparison.model_validate_json(arguments.observations.read_bytes())
        summary = score_comparison(
            comparison,
            manifest,
            hashlib.sha256(arguments.manifest.read_bytes()).hexdigest(),
        )
    except OSError, ValidationError, CorpusValidationError, RetrievalObservationError:
        print("M1.5 paired retrieval comparison failed; details withheld.", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
