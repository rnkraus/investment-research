"""Measure reviewed evidence markers in saved whole-archive retrieval selections."""

import argparse
import hashlib
import json
import sys
import unicodedata
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CATALOG = Path(__file__).resolve().parents[2] / "eval" / "portfolio-cases-v1.json"
ShortText = Annotated[str, Field(min_length=1)]
Page = Annotated[int, Field(strict=True, ge=1)]


class CatalogModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class Target(CatalogModel):
    id: ShortText
    source: ShortText
    pages: Annotated[tuple[Page | None, ...], Field(min_length=1)]
    markers: Annotated[tuple[ShortText, ...], Field(min_length=1)]


class Case(CatalogModel):
    id: Annotated[str, Field(pattern=r"^[DAU][0-9]{2}$")]
    kind: Literal["discovery", "answer", "abstention"]
    question: ShortText
    expected: ShortText
    discovery_scope: ShortText | None = None
    document_scope: tuple[ShortText, ...] = ()
    context_targets: tuple[Target, ...] = ()
    whole_archive_replay_question: ShortText | None = None


class Catalog(CatalogModel):
    version: Literal[1]
    sources: dict[ShortText, ShortText]
    cases: tuple[Case, ...]


class Candidate(BaseModel):
    id: Annotated[int, Field(strict=True, ge=1)]
    title: ShortText
    page: Page | None
    text: ShortText


class Pool(BaseModel):
    case: ShortText
    question: ShortText
    selector: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,100}$")]
    app: Annotated[tuple[Annotated[int, Field(strict=True, ge=1)], ...], Field(max_length=8)]
    candidates: tuple[Candidate, ...]


class SnapshotError(ValueError):
    """Report invalid input without reproducing document text."""


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def matches_target(target: Target, page: int | None, normalized_text: str) -> bool:
    """Match evidence markers in one passage from an already matched source."""
    return page in target.pages and all(
        normalized(marker) in normalized_text for marker in target.markers
    )


def validate_catalog(catalog: Catalog) -> None:
    expected_ids = {
        **{f"D{i:02}": "discovery" for i in range(1, 8)},
        **{f"A{i:02}": "answer" for i in range(1, 10)},
        **{f"U{i:02}": "abstention" for i in range(1, 4)},
    }
    if (
        len(catalog.cases) != len(expected_ids)
        or {case.id: case.kind for case in catalog.cases} != expected_ids
    ):
        raise SnapshotError("expected 19 unique reviewed case IDs and kinds")
    if len(set(catalog.sources.values())) != len(catalog.sources):
        raise SnapshotError("source titles must be unique")
    for case in catalog.cases:
        if case.kind == "discovery":
            if not case.discovery_scope or case.document_scope or case.context_targets:
                raise SnapshotError(f"{case.id}: invalid discovery scope")
            continue
        if (
            case.discovery_scope
            or not case.document_scope
            or not case.context_targets
            or not set(case.document_scope) <= catalog.sources.keys()
            or len({target.id for target in case.context_targets}) != len(case.context_targets)
            or any(target.source not in case.document_scope for target in case.context_targets)
        ):
            raise SnapshotError(f"{case.id}: invalid source scope or evidence targets")
        if any(
            not normalized(marker) for target in case.context_targets for marker in target.markers
        ):
            raise SnapshotError(f"{case.id}: evidence markers must contain visible text")


def score(catalog: Catalog, snapshot: bytes) -> dict[str, object]:
    validate_catalog(catalog)
    cases = {case.id: case for case in catalog.cases if case.kind != "discovery"}
    observed: set[str] = set()
    selectors: set[str] = set()
    rows: list[dict[str, object]] = []
    fully_covered = 0
    for line in snapshot.splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if isinstance(raw, dict) and (
            raw.get("case") == "IREN" or set(raw) == {"completed", "provider_calls"}
        ):
            continue  # The saved export also contains a separate location case and metadata.
        pool = Pool.model_validate(raw)
        if pool.case in observed or pool.case not in cases:
            raise SnapshotError("unexpected or duplicate case in retrieval snapshot")
        case = cases[pool.case]
        if pool.question != (case.whole_archive_replay_question or case.question):
            raise SnapshotError(f"{case.id}: question differs from the reviewed replay question")
        observed.add(case.id)
        selectors.add(pool.selector)
        by_id = {candidate.id: candidate for candidate in pool.candidates}
        if (
            len(by_id) != len(pool.candidates)
            or len(set(pool.app)) != len(pool.app)
            or not set(pool.app) <= by_id.keys()
        ):
            raise SnapshotError(f"{case.id}: invalid candidate IDs or selection")
        selected = [(by_id[id_], normalized(by_id[id_].text)) for id_ in pool.app]
        ranks = {
            target.id: [
                rank
                for rank, (candidate, text) in enumerate(selected, start=1)
                if candidate.title == catalog.sources[target.source]
                and matches_target(target, candidate.page, text)
            ]
            for target in case.context_targets
        }
        all_found = all(ranks.values())
        if case.kind == "answer":
            fully_covered += all_found
        rows.append(
            {
                "case": case.id,
                "kind": case.kind,
                "selector": pool.selector,
                "selected_passages": len(selected),
                "evidence_marker_ranks": ranks,
            }
        )
    if observed != cases.keys():
        raise SnapshotError("snapshot must contain all 12 research cases")
    return {
        "catalog_version": catalog.version,
        "snapshot_sha256": hashlib.sha256(snapshot).hexdigest(),
        "scope": "whole_archive",
        "stage": "saved_selection_before_generation_preflight",
        "selectors": sorted(selectors),
        "discovery_cases_without_context_scores": 7,
        "answer_cases_with_all_markers": fully_covered,
        "answer_cases": 9,
        "abstention_cases_with_context_diagnostics": 3,
        "cases": sorted(rows, key=lambda row: str(row["case"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="Saved app-rerank-pools JSONL")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args()
    try:
        corpus = args.catalog.read_bytes()
        catalog = Catalog.model_validate_json(corpus)
        report = score(catalog, args.snapshot.read_bytes())
    except SnapshotError as error:
        print(f"Portfolio retrieval check failed: {error}", file=sys.stderr)
        return 1
    except OSError, ValueError:
        print("Portfolio retrieval check failed: invalid or unreadable input", file=sys.stderr)
        return 1
    report["catalog_sha256"] = hashlib.sha256(corpus).hexdigest()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
