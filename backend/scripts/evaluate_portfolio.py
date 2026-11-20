"""Score or compare recorded selections offline; no retrieval, model calls, or answer grading."""

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from scripts.evaluation_profiles import (
    DEFAULT_PROFILE,
    EvaluationProfileError,
    SelectionDefinition,
    checked_repository_file,
    resolve_profile,
)
from scripts.portfolio_snapshots import (
    EvaluationSnapshot,
    OfflineEvaluationError,
    RecordedPassage,
    RecordedSelection,
    compatible_passages,
    corpus_digest,
    describe_passage,
    evidence_ranks,
    read_snapshot,
    score_selection,
    selected_rows,
    selection_key,
)


@dataclass(frozen=True)
class OfflineResult:
    resolved: SelectionDefinition
    snapshot: EvaluationSnapshot
    rows: tuple[RecordedSelection, ...]
    report: dict[str, object]


def evaluate_saved(
    profile_path: Path = DEFAULT_PROFILE,
    *,
    snapshot_reference: str | None = None,
    case_ids: tuple[str, ...] = (),
) -> OfflineResult:
    resolved = resolve_profile(profile_path, case_ids=case_ids)
    if resolved.profile.mode != "offline_replay":
        raise OfflineEvaluationError("this executor supports offline_replay only")
    reference = snapshot_reference or resolved.profile.inputs.snapshot
    if not reference:
        raise OfflineEvaluationError("choose a saved snapshot in inputs.snapshot or --snapshot")
    path = checked_repository_file(reference, subtree="outputs", suffixes=(".json",))
    snapshot, raw = read_snapshot(path)
    return score_snapshot(resolved, snapshot, raw)


def score_snapshot(
    resolved: SelectionDefinition,
    snapshot: EvaluationSnapshot,
    raw: bytes,
    *,
    require_complete: bool = True,
) -> OfflineResult:
    """Score a validated frozen selection without loading current configuration or providers."""

    rows = selected_rows(resolved, snapshot, require_complete=require_complete)
    cases = {case.id: case for case in resolved.cases}
    observations = [score_selection(cases[row.case], row) for row in rows]
    summary = {
        "selected_cases": len(cases),
        "observations": len(rows),
        "answer_observations": sum(cases[row.case].kind == "answer" for row in rows),
        "answer_observations_with_all_markers": sum(
            item["kind"] == "answer" and item["all_markers_found"] is True for item in observations
        ),
        "discovery_observations": sum(row.archive_mode is not None for row in rows),
        "abstention_observations": sum(cases[row.case].kind == "abstention" for row in rows),
    }
    report = {
        "schema_version": 1,
        "execution_mode": "recorded_selection_scoring_offline",
        "provider_calls_made": 0,
        "estimated_new_provider_cost_micro_usd": 0,
        "name": resolved.profile.name,
        "profile_sha256": hashlib.sha256(resolved.profile_bytes).hexdigest(),
        "catalog_sha256": snapshot.catalog_sha256,
        "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
        "corpus_sha256": corpus_digest(snapshot),
        "recorded_source_count": len(snapshot.sources),
        "sources_with_original_hash": sum(
            source.original_sha256 is not None for source in snapshot.sources
        ),
        "recorded_code_sha256": snapshot.code_sha256,
        "recorded_versions": snapshot.recorded_versions,
        "settings": snapshot.settings.model_dump(mode="json"),
        "summary": summary,
        "observations": observations,
        "limitations": [
            "Recorded selections are scored, not recomputed; switches must match the recording.",
            "Snapshot integrity is checked locally, not current canonical database eligibility.",
            "Marker coverage does not establish answer completeness, claim support, or abstention.",
            "Discovery expectations are prose and require manual review of the recorded results.",
            "Repeated saved inputs do not measure new model variability or live latency/cost.",
            "No source passages, prompts, or raw runtime identifiers are included in this report.",
        ],
    }
    return OfflineResult(resolved, snapshot, rows, report)


def compare_saved(candidate: OfflineResult, baseline: OfflineResult) -> dict[str, object]:
    if candidate.snapshot.catalog_sha256 != baseline.snapshot.catalog_sha256 or corpus_digest(
        candidate.snapshot
    ) != corpus_digest(baseline.snapshot):
        raise OfflineEvaluationError("comparisons require the same catalog and corpus identities")
    left = {selection_key(row): row for row in baseline.rows}
    right = {selection_key(row): row for row in candidate.rows}
    if (
        left.keys() != right.keys()
        or candidate.snapshot.settings.scope != baseline.snapshot.settings.scope
    ):
        raise OfflineEvaluationError(
            "comparisons require identical selected cases, modes, and scope"
        )
    old_chunks: dict[UUID, RecordedPassage] = {}
    for row in baseline.rows:
        for item in row.selected:
            previous = old_chunks.get(item.chunk_id)
            if previous is None or len(item.text) > len(previous.text):
                old_chunks[item.chunk_id] = item
    if any(
        not compatible_passages(item, old_chunks[item.chunk_id])
        for row in candidate.rows
        for item in row.selected
        if item.chunk_id in old_chunks
    ):
        raise OfflineEvaluationError("a shared immutable chunk has conflicting recorded content")
    cases = {case.id: case for case in candidate.resolved.cases}
    comparisons = []
    for key, after in right.items():
        before = left[key]
        if before.stage != after.stage:
            raise OfflineEvaluationError("cannot compare selections from different pipeline stages")
        old = evidence_ranks(cases[after.case], before)
        new = evidence_ranks(cases[after.case], after)
        old_ids = {item.chunk_id for item in before.selected}
        new_ids = {item.chunk_id for item in after.selected}
        comparable_pool = (
            before.candidate_pool_sha256 == after.candidate_pool_sha256
            if before.candidate_pool_sha256 is not None and after.candidate_pool_sha256 is not None
            else None
        )
        comparisons.append(
            {
                "case": after.case,
                "archive_mode": after.archive_mode,
                "repetition": after.repetition,
                "gained_evidence_markers": [name for name in new if new[name] and not old[name]],
                "lost_evidence_markers": [name for name in old if old[name] and not new[name]],
                "rank_changes": {
                    name: {"before": old[name], "after": new[name]}
                    for name in old
                    if old[name] != new[name]
                },
                "added_passages": [
                    describe_passage(item)
                    for item in after.selected
                    if item.chunk_id not in old_ids
                ],
                "removed_passages": [
                    describe_passage(item)
                    for item in before.selected
                    if item.chunk_id not in new_ids
                ],
                "same_candidate_pool": comparable_pool,
            }
        )
    return {
        "schema_version": 1,
        "execution_mode": "recorded_selection_comparison_offline",
        "provider_calls_made": 0,
        "baseline": baseline.report,
        "candidate": candidate.report,
        "comparisons": comparisons,
        "limitation": "Observed selection differences are not causal or answer-quality proof; "
        "inspect candidate-pool control and recorded versions.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--snapshot", help="Override the profile's local outputs/ snapshot path")
    parser.add_argument("--case", action="append", default=[], help="Select catalog IDs explicitly")
    parser.add_argument("--compare-profile", type=Path)
    parser.add_argument("--compare-snapshot")
    parser.add_argument("--output", help="New .json report beneath outputs/; never overwrite")
    args = parser.parse_args()
    if args.compare_snapshot and args.compare_profile is None:
        parser.error("--compare-snapshot requires --compare-profile")
    try:
        candidate = evaluate_saved(
            args.profile, snapshot_reference=args.snapshot, case_ids=tuple(args.case)
        )
        report = candidate.report
        if args.compare_profile is not None:
            baseline = evaluate_saved(
                args.compare_profile,
                snapshot_reference=args.compare_snapshot,
                case_ids=tuple(args.case),
            )
            report = compare_saved(candidate, baseline)
        rendered = json.dumps(report, indent=2)
        if args.output:
            target = checked_repository_file(args.output, subtree="outputs", suffixes=(".json",))
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as output:
                output.write(rendered + "\n")
            print(
                json.dumps(
                    {
                        "report_written": True,
                        "provider_calls_made": 0,
                        "summary": candidate.report["summary"],
                    }
                )
            )
        else:
            print(rendered)
    except EvaluationProfileError, OfflineEvaluationError:
        print(
            "Offline evaluation rejected: profile, snapshot, or comparison is incompatible",
            file=sys.stderr,
        )
        return 1
    except OSError, ValueError:
        print(
            "Offline evaluation rejected: invalid input or unavailable output; "
            "existing files are preserved",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
