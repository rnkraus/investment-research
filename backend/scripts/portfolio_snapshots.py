"""Strict local snapshot contracts and marker scoring; never retrieve or generate content."""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from investment_research.rag_settings import RagSettings
from scripts.evaluation_profiles import (
    CaseId,
    ModelSettings,
    ProfileModel,
    ResolvedEvaluationProfile,
)
from scripts.score_portfolio_retrieval import Case, matches_target, normalized

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_.-]+$")]
ArchiveMode = Literal["keyword", "semantic", "hybrid"]


class OfflineEvaluationError(ValueError):
    """A content-free failure; never expose saved passages through an exception."""


class RecordedSettings(ProfileModel):
    scope: Literal["whole_archive", "catalog_documents"]
    lexical_strategy: Literal["websearch", "weighted_or"]
    text_neighbors: bool
    page_expansion: bool
    company_priority: bool
    decompose_query: bool
    reranking: Literal["none", "luna", "pinecone"]
    models: ModelSettings
    rag: RagSettings

    @model_validator(mode="before")
    @classmethod
    def require_recorded_limits(cls, value: object) -> object:
        required = {
            "retrieval": {"lexical_candidate_limit", "dense_candidate_limit"},
            "context": {"chunk_limit", "character_limit"},
            "generation": {"input_overhead_limit"},
        }
        if isinstance(value, dict) and isinstance(value.get("rag"), RagSettings):
            value = value | {"rag": value["rag"].model_dump()}
        if not isinstance(value, dict) or not isinstance(rag := value.get("rag"), dict):
            raise ValueError("snapshot requires explicit resolved RAG settings")
        for section, keys in required.items():
            if not isinstance(content := rag.get(section), dict) or not keys <= content.keys():
                raise ValueError("snapshot requires every resolved RAG limit")
        return value


def settings_for(resolved: ResolvedEvaluationProfile) -> RecordedSettings:
    profile = resolved.profile
    return RecordedSettings(
        scope=profile.scope,
        lexical_strategy=profile.retrieval.lexical_strategy,
        text_neighbors=profile.retrieval.text_neighbors,
        page_expansion=profile.retrieval.page_expansion,
        company_priority=profile.retrieval.company_priority,
        decompose_query=profile.retrieval.decompose_query,
        reranking=profile.reranking.strategy,
        models=profile.models,
        rag=resolved.rag,
    )


class RecordedSource(ProfileModel):
    alias: Identifier
    document_id: UUID
    document_version_id: UUID
    title: str | None = Field(default=None, min_length=1, max_length=1_000)
    original_sha256: Digest | None = None


class RecordedPassage(ProfileModel):
    chunk_id: UUID
    source: Identifier
    page: int | None = Field(default=None, ge=1)
    text: str = Field(min_length=1, max_length=100_000, repr=False)


class RecordedSelection(ProfileModel):
    case: CaseId
    question: str = Field(min_length=1, max_length=4_000, repr=False)
    repetition: int = Field(default=1, ge=1, le=10)
    archive_mode: ArchiveMode | None = None
    stage: Literal[
        "final_generation_context", "selection_before_generation_preflight", "archive_results"
    ]
    candidate_pool_sha256: Digest | None = None
    selected: tuple[RecordedPassage, ...] = Field(max_length=20)

    @model_validator(mode="after")
    def require_unique_bounded_selection(self) -> Self:
        if len({item.chunk_id for item in self.selected}) != len(self.selected):
            raise ValueError("selected chunk IDs must be unique")
        if (self.archive_mode is not None) != (self.stage == "archive_results"):
            raise ValueError("only archive results may specify a discovery mode")
        if self.archive_mode is None and len(self.selected) > 8:
            raise ValueError("research selection exceeds eight chunks")
        return self


class EvaluationSnapshot(ProfileModel):
    schema_version: int = Field(ge=1, le=1)
    catalog_sha256: Digest
    code_sha256: Digest
    recorded_versions: dict[Identifier, Identifier]
    settings: RecordedSettings
    sources: tuple[RecordedSource, ...] = Field(min_length=1, max_length=10_000)
    selections: tuple[RecordedSelection, ...] = Field(min_length=1, max_length=3_000)

    @model_validator(mode="after")
    def require_consistent_source_and_case_identity(self) -> Self:
        aliases = {source.alias for source in self.sources}
        if (
            len(aliases) != len(self.sources)
            or len({source.document_id for source in self.sources}) != len(self.sources)
            or len({source.document_version_id for source in self.sources}) != len(self.sources)
        ):
            raise ValueError("snapshot sources must be unique")
        keys = [selection_key(row) for row in self.selections]
        if len(set(keys)) != len(keys):
            raise ValueError("snapshot selection keys must be unique")
        chunks: dict[UUID, RecordedPassage] = {}
        for selection in self.selections:
            if selection.archive_mode is None and (
                len(selection.selected) > self.settings.rag.context.chunk_limit
                or sum(len(passage.text) for passage in selection.selected)
                > self.settings.rag.context.character_limit
            ):
                raise ValueError("saved research context exceeds its recorded limits")
            for passage in selection.selected:
                if passage.source not in aliases or not passage.text.strip():
                    raise ValueError("passage has an unknown source or empty text")
                if passage.chunk_id in chunks and chunks[passage.chunk_id] != passage:
                    raise ValueError("one chunk ID has conflicting saved content")
                chunks[passage.chunk_id] = passage
        return self


def selection_key(row: RecordedSelection) -> tuple[str, str | None, int]:
    return row.case, row.archive_mode, row.repetition


def corpus_digest(snapshot: EvaluationSnapshot) -> str:
    identities = sorted(
        (str(source.document_id), str(source.document_version_id), source.original_sha256)
        for source in snapshot.sources
    )
    return hashlib.sha256(json.dumps(identities, separators=(",", ":")).encode()).hexdigest()


def read_snapshot(path: Path) -> tuple[EvaluationSnapshot, bytes]:
    with path.open("rb") as stream:
        content = stream.read(50 * 1_024 * 1_024 + 1)
    if len(content) > 50 * 1_024 * 1_024:
        raise OfflineEvaluationError("snapshot exceeds the 50 MiB input limit")
    return EvaluationSnapshot.model_validate_json(content), content


def selected_rows(
    resolved: ResolvedEvaluationProfile, snapshot: EvaluationSnapshot
) -> tuple[RecordedSelection, ...]:
    if hashlib.sha256(resolved.catalog_bytes).hexdigest() != snapshot.catalog_sha256:
        raise OfflineEvaluationError("snapshot catalog differs from the selected catalog")
    if settings_for(resolved) != snapshot.settings:
        raise OfflineEvaluationError("profile settings differ from the recorded selection")
    catalog_cases = {case.id: case for case in resolved.catalog.cases}
    for source in snapshot.sources:
        if source.alias in resolved.catalog.sources and (
            source.title != resolved.catalog.sources[source.alias]
        ):
            raise OfflineEvaluationError("a catalog source alias has a different recorded title")
    sources = {source.alias for source in snapshot.sources}
    for case in resolved.cases:
        if not set(case.document_scope) <= sources:
            raise OfflineEvaluationError("snapshot is missing a selected case's declared sources")
    for selection in snapshot.selections:
        saved_case = catalog_cases.get(selection.case)
        if saved_case is None or selection.question != saved_case.question:
            raise OfflineEvaluationError("saved case or question differs from the reviewed catalog")
        if (saved_case.kind == "discovery") != (selection.archive_mode is not None):
            raise OfflineEvaluationError("saved selection does not match its catalog case kind")
        if (
            saved_case.kind != "discovery"
            and snapshot.settings.scope == "catalog_documents"
            and any(
                passage.source not in saved_case.document_scope for passage in selection.selected
            )
        ):
            raise OfflineEvaluationError("saved passage lies outside the declared document scope")
    index = {selection_key(row): row for row in snapshot.selections}
    rows = []
    for case in resolved.cases:
        modes = resolved.profile.archive.modes if case.kind == "discovery" else (None,)
        for mode in modes:
            for repetition in range(1, resolved.profile.repetitions + 1):
                row = index.get((case.id, mode, repetition))
                if row is None:
                    raise OfflineEvaluationError(
                        "snapshot lacks a selected case, mode, or repetition"
                    )
                rows.append(row)
    return tuple(rows)


def evidence_ranks(case: Case, row: RecordedSelection) -> dict[str, list[int]]:
    return {
        target.id: [
            rank
            for rank, passage in enumerate(row.selected, 1)
            if passage.source == target.source
            and matches_target(target, passage.page, normalized(passage.text))
        ]
        for target in case.context_targets
    }


def describe_passage(item: RecordedPassage) -> dict[str, object]:
    return {
        "chunk_fingerprint": hashlib.sha256(str(item.chunk_id).encode()).hexdigest(),
        "source": item.source,
        "page": item.page,
    }


def score_selection(case: Case, row: RecordedSelection) -> dict[str, object]:
    ranks = evidence_ranks(case, row)
    return {
        "case": case.id,
        "kind": case.kind,
        "archive_mode": row.archive_mode,
        "repetition": row.repetition,
        "stage": row.stage,
        "selected_count": len(row.selected),
        "evidence_marker_ranks": ranks,
        "all_markers_found": all(ranks.values()) if case.kind == "answer" else None,
        "selected": [describe_passage(item) for item in row.selected],
        "manual_review_required": True,
    }
