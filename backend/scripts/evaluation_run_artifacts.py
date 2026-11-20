"""Read a frozen evaluation bundle without current configuration or provider initialization."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from tomllib import loads
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from investment_research.rag_settings import RagSettings
from scripts.evaluate_portfolio import OfflineResult, score_snapshot
from scripts.evaluation_profiles import (
    CaseId,
    EvaluationProfile,
    checked_repository_file,
    effective_rag_settings,
    select_catalog_cases,
)
from scripts.portfolio_snapshots import (
    ArchiveMode,
    Digest,
    RecordedSettings,
    read_snapshot,
    selection_key,
    settings_for,
)
from scripts.score_portfolio_retrieval import Case, Catalog, validate_catalog


class RunArtifactError(ValueError):
    """Reject incomplete or inconsistent artifacts without printing their contents."""


class ArtifactModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="ignore", hide_input_in_errors=True)


class SavedPlan(ArtifactModel):
    mode: Literal["retrieval_live", "rag_live"]
    case_ids: tuple[CaseId, ...] = Field(min_length=1, max_length=1_000)
    archive_modes: tuple[ArchiveMode, ...] | None = None
    settings: RecordedSettings


class SavedManifest(ArtifactModel):
    plan: SavedPlan
    status: Literal["completed", "incomplete", "budget_exhausted", "failed"]
    profile_sha256: Digest
    catalog_sha256: Digest
    code_sha256: Digest
    observations: int = Field(ge=0)
    failed_observations: int = Field(ge=0)
    provider_calls: int = Field(ge=0)
    reserved_model_cost_micro_usd: int = Field(ge=0)


class SavedObservation(ArtifactModel):
    case: CaseId
    repetition: int = Field(default=1, ge=1, le=10)
    archive_mode: ArchiveMode | None = None
    status: Literal["retrieved", "searched", "STREAMED", "INSUFFICIENT", "REJECTED", "failed"]
    elapsed_ms: int = Field(ge=0)

    @property
    def key(self) -> tuple[str, str | None, int]:
        return self.case, self.archive_mode, self.repetition


@dataclass(frozen=True)
class FrozenDefinition:
    profile: EvaluationProfile
    profile_bytes: bytes
    catalog: Catalog
    catalog_bytes: bytes
    cases: tuple[Case, ...]
    rag: RagSettings


@dataclass(frozen=True)
class SavedRun:
    directory: Path
    manifest: SavedManifest
    definition: FrozenDefinition
    observations: tuple[SavedObservation, ...]
    scored: OfflineResult | None

    @property
    def expected_keys(self) -> tuple[tuple[str, str | None, int], ...]:
        return tuple(
            (case.id, mode, repetition)
            for repetition in range(1, self.definition.profile.repetitions + 1)
            for case in self.definition.cases
            for mode in (
                self.definition.profile.archive.modes if case.kind == "discovery" else (None,)
            )
        )


def run_directory(reference: str) -> Path:
    return checked_repository_file(
        reference.rstrip("/") + "/manifest.json", subtree="outputs", suffixes=(".json",)
    ).parent


def bounded_read(reference: str, name: str, limit: int) -> bytes:
    path = checked_repository_file(
        reference.rstrip("/") + "/" + name,
        subtree="outputs",
        suffixes=(".toml", ".json", ".jsonl"),
    )
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise RunArtifactError("evaluation artifact exceeds its input limit")
    return content


def load_run(reference: str) -> SavedRun:
    directory = run_directory(reference)
    manifest = SavedManifest.model_validate_json(
        bounded_read(reference, "manifest.json", 4_000_000)
    )
    profile_bytes = bounded_read(reference, "profile.toml", 256_000)
    catalog_bytes = bounded_read(reference, "catalog.json", 2_000_000)
    if (
        hashlib.sha256(profile_bytes).hexdigest() != manifest.profile_sha256
        or hashlib.sha256(catalog_bytes).hexdigest() != manifest.catalog_sha256
    ):
        raise RunArtifactError("recorded profile or catalog digest does not match")
    profile = EvaluationProfile.model_validate(loads(profile_bytes.decode("utf-8")))
    # --case overrides are recorded in the plan even when the original TOML selects more cases.
    if len(set(manifest.plan.case_ids)) != len(manifest.plan.case_ids):
        raise RunArtifactError("recorded case selection contains duplicates")
    profile = EvaluationProfile.model_validate(
        profile.model_dump()
        | {
            "cases": {"include": manifest.plan.case_ids, "exclude": ()},
        }
    )
    catalog = Catalog.model_validate_json(catalog_bytes)
    validate_catalog(catalog, require_portfolio_cases=False)
    cases = select_catalog_cases(profile, catalog)
    definition = FrozenDefinition(
        profile,
        profile_bytes,
        catalog,
        catalog_bytes,
        cases,
        manifest.plan.settings.rag,
    )
    if (
        profile.mode != manifest.plan.mode
        or (
            manifest.plan.archive_modes is not None
            and profile.archive.modes != manifest.plan.archive_modes
        )
        or effective_rag_settings(profile, definition.rag) != definition.rag
        or settings_for(definition) != manifest.plan.settings
        or tuple(case.id for case in cases) != manifest.plan.case_ids
    ):
        raise RunArtifactError("recorded plan differs from its frozen configuration")
    journal = bounded_read(reference, "results.jsonl", 50 * 1_024 * 1_024)
    lines = [line for line in journal.splitlines() if line.strip()]
    if len(lines) > 3_000:
        raise RunArtifactError("evaluation journal exceeds its observation limit")
    observations = tuple(SavedObservation.model_validate_json(line) for line in lines)
    scored = None
    snapshot_path = checked_repository_file(
        reference.rstrip("/") + "/snapshot.json",
        subtree="outputs",
        suffixes=(".json",),
    )
    if snapshot_path.exists():
        snapshot, raw = read_snapshot(snapshot_path)
        if snapshot.code_sha256 != manifest.code_sha256:
            raise RunArtifactError("recorded code identities differ")
        scored = score_snapshot(definition, snapshot, raw, require_complete=False)
    result = SavedRun(directory, manifest, definition, observations, scored)
    expected = set(result.expected_keys)
    recorded = {item.key: item for item in observations}
    if (
        len(recorded) != len(observations)
        or not recorded.keys() <= expected
        or manifest.observations != len(observations)
        or manifest.failed_observations != sum(item.status == "failed" for item in observations)
        or (manifest.status == "completed" and recorded.keys() != expected)
    ):
        raise RunArtifactError("journal counts or observations differ from the plan")
    rows = {selection_key(row): row for row in scored.rows} if scored else {}
    if not rows.keys() <= recorded.keys() or any(
        item.status != "failed" and item.key not in rows for item in observations
    ):
        raise RunArtifactError("successful observations require their recorded context")
    for item in observations:
        allowed = (
            {"searched", "failed"}
            if item.archive_mode is not None
            else {"retrieved", "failed"}
            if profile.mode == "retrieval_live"
            else {"STREAMED", "INSUFFICIENT", "REJECTED", "failed"}
        )
        if item.status not in allowed:
            raise RunArtifactError("recorded status does not match the selected mode")
    return result
