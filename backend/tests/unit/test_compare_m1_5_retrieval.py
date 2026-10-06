"""Validate paired offline comparison reports without treating controlled inputs as measurements."""

import hashlib
import json
import sys
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.search.contracts import M1_SELECTOR_CONFIG_VERSION
from scripts.compare_m1_5_retrieval import (
    ContextObservation,
    PairedCaseObservation,
    RetrievalComparison,
    main,
    score_comparison,
)
from scripts.score_m1_5_retrieval import RetrievalConfiguration, RetrievalObservationError
from scripts.verify_m1_5_corpus import DEFAULT_MANIFEST, REPOSITORY_ROOT, validate_corpus


def controlled_comparison() -> RetrievalComparison:
    """Construct scorer inputs only; the values are not a measured quality result."""

    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    pairs = []
    for index, case in enumerate(manifest.cases):
        selected = tuple(group[0] for group in case.evidence_groups)
        context = ContextObservation(
            case_id=case.id,
            selected_passage_ids=selected,
            selected_chunk_ids=tuple(
                UUID(int=100 * (index + 1) + rank) for rank in range(1, len(selected) + 1)
            ),
            selected_chunk_count=len(selected),
            selected_character_count=10 * len(selected),
            lexical_hit_count=len(selected),
            qualified_dense_hit_count=len(selected),
            evidence_group_first_ranks=tuple(range(1, len(selected) + 1)),
            selector_elapsed_ns=1_000,
            elapsed_ms=0,
        )
        pairs.append(
            PairedCaseObservation(
                qualified_lanes_sha256="a" * 64,
                shared_retrieval_elapsed_ns=0,
                m1=context,
                rrf=context,
            )
        )
    return RetrievalComparison(
        corpus_id=manifest.id,
        corpus_sha256=hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest(),
        parser_version="synthetic-parser-1",
        parser_config_version="synthetic-parser-v1",
        chunker_version="synthetic-chunker-1",
        chunking_config_version="synthetic-chunk-v1",
        configuration=RetrievalConfiguration(
            lexical_config_version="m1-english-lexical-v1",
            embedding_config_version="synthetic-embedding-v1",
            dense_index_config_version="synthetic-index-v1",
            selector_config_version=M1_SELECTOR_CONFIG_VERSION,
            embedding_adapter="deterministic_rules",
            dense_adapter="deterministic_sqlite_cosine",
        ),
        cases=tuple(pairs),
    )


def score(comparison: RetrievalComparison) -> dict[str, object]:
    return score_comparison(
        comparison,
        validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT),
        hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest(),
    )


def test_comparison_keeps_both_metrics_separate_and_does_not_choose_a_default() -> None:
    summary = score(controlled_comparison())
    for policy in ("m1", "rrf"):
        measured = summary[policy]
        assert isinstance(measured, dict)
        assert measured["evidence_available_count"] == 5
        assert measured["answerable_case_count"] == 5
        assert "coverage_match_count" not in measured
        assert "estimated_cost_micro_usd" not in measured
    assert summary["selection_changed_count"] == 0
    assert summary["selected_set_changed_count"] == 0
    assert summary["default_policy_changed"] is False


def test_comparison_reports_lost_evidence_instead_of_hiding_a_regression() -> None:
    comparison = controlled_comparison()
    first = comparison.cases[0]
    rrf = first.rrf.model_copy(
        update={
            "selected_passage_ids": (),
            "evidence_group_first_ranks": (None,),
            "selected_chunk_ids": (UUID(int=999),),
        }
    )
    changed = comparison.model_copy(
        update={
            "cases": (
                first.model_copy(update={"rrf": rrf}),
                *comparison.cases[1:],
            )
        }
    )
    summary = score(changed)
    measured = summary["rrf"]
    assert isinstance(measured, dict)
    assert measured["evidence_available_count"] == 4
    assert summary["selected_set_changed_count"] == 1


def test_comparison_distinguishes_reordering_from_changed_context_membership() -> None:
    comparison = controlled_comparison()
    index = next(
        i for i, item in enumerate(comparison.cases) if len(item.m1.evidence_group_first_ranks) == 2
    )
    pair = comparison.cases[index]
    rrf = pair.rrf.model_copy(
        update={
            "selected_chunk_ids": pair.rrf.selected_chunk_ids[::-1],
            "evidence_group_first_ranks": pair.rrf.evidence_group_first_ranks[::-1],
        }
    )
    changed = comparison.model_copy(
        update={
            "cases": (
                *comparison.cases[:index],
                pair.model_copy(update={"rrf": rrf}),
                *comparison.cases[index + 1 :],
            )
        }
    )
    summary = score(changed)
    assert summary["selection_changed_count"] == 1
    assert summary["selected_set_changed_count"] == 0


@pytest.mark.parametrize("duplicate", [False, True])
def test_comparison_requires_all_six_cases_exactly_once(duplicate: bool) -> None:
    comparison = controlled_comparison()
    changed = comparison.model_copy(
        update={
            "cases": (
                (*comparison.cases[:-1], comparison.cases[0])
                if duplicate
                else comparison.cases[:-1]
            )
        }
    )
    with pytest.raises(RetrievalObservationError, match="every case exactly once"):
        score(changed)


def test_comparison_rejects_a_stale_corpus_digest() -> None:
    comparison = controlled_comparison().model_copy(update={"corpus_sha256": "0" * 64})
    with pytest.raises(RetrievalObservationError, match="corpus version"):
        score(comparison)


@pytest.mark.parametrize("field", ["case_id", "lexical_hit_count", "qualified_dense_hit_count"])
def test_pair_rejects_different_case_or_lane_counts(field: str) -> None:
    pair = controlled_comparison().cases[0]
    changed = pair.rrf.model_dump() | {field: "other" if field == "case_id" else 2}
    with pytest.raises(ValidationError, match="share the case and lane counts"):
        PairedCaseObservation.model_validate(pair.model_dump() | {"rrf": changed})


def test_context_rejects_duplicate_chunk_ids_and_out_of_range_evidence_positions() -> None:
    context = controlled_comparison().cases[0].m1
    with pytest.raises(ValidationError, match="unique and match their count"):
        ContextObservation.model_validate(
            context.model_dump()
            | {
                "selected_chunk_ids": context.selected_chunk_ids * 2,
                "selected_chunk_count": 2,
            }
        )
    with pytest.raises(ValidationError, match="exceeds the selected context"):
        ContextObservation.model_validate(
            context.model_dump() | {"evidence_group_first_ranks": (2,)}
        )


def test_pair_rejects_inflated_elapsed_time() -> None:
    pair = controlled_comparison().cases[0]
    with pytest.raises(ValidationError, match="elapsed time"):
        PairedCaseObservation.model_validate(
            pair.model_dump()
            | {
                "rrf": pair.rrf.model_dump() | {"elapsed_ms": 10},
            }
        )


def test_comparison_rejects_rank_labels_that_hide_missing_evidence() -> None:
    comparison = controlled_comparison()
    pair = comparison.cases[0]
    rrf = pair.rrf.model_copy(update={"evidence_group_first_ranks": (None,)})
    changed = comparison.model_copy(
        update={
            "cases": (
                pair.model_copy(update={"rrf": rrf}),
                *comparison.cases[1:],
            )
        }
    )
    with pytest.raises(RetrievalObservationError, match="reviewed group mappings"):
        score(changed)


@pytest.mark.parametrize(
    "changes",
    [
        {"context_chunk_limit": 7},
        {"context_character_limit": 20_000},
        {"rrf_selector_config_version": "other"},
        {"answer_text": "must-not-be-retained"},
    ],
)
def test_comparison_rejects_changed_budgets_and_raw_content(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RetrievalComparison.model_validate(controlled_comparison().model_dump() | changes)


def test_comparison_requires_the_frozen_m1_selector() -> None:
    comparison = controlled_comparison()
    with pytest.raises(ValidationError, match="frozen M1 selector"):
        RetrievalComparison.model_validate(
            comparison.model_dump()
            | {
                "configuration": comparison.configuration.model_dump()
                | {"selector_config_version": "other"},
            }
        )


def test_cli_emits_only_safe_counts_and_rank_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "comparison.json"
    path.write_text(controlled_comparison().model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["compare_m1_5_retrieval", "--observations", str(path)])
    assert main() == 0
    output = capsys.readouterr().out
    summary = json.loads(output)
    assert summary["m1"]["case_count"] == summary["rrf"]["case_count"] == 6
    assert "Northstar" not in output
    assert "transformers" not in output


def test_cli_withholds_invalid_report_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text('{"source_text":"private-marker"}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["compare_m1_5_retrieval", "--observations", str(path)])
    assert main() == 1
    output = capsys.readouterr()
    assert "private-marker" not in output.err + output.out
