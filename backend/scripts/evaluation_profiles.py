"""Load non-secret evaluation settings without initializing an application or provider."""

from decimal import Decimal
from pathlib import Path
from tomllib import TOMLDecodeError, loads
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from investment_research.rag_settings import RagSettings

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = REPOSITORY_ROOT / "eval/profiles/standard.example.toml"
ModelName = Literal["gpt-5.6-terra", "gpt-5.6-luna"]
CaseId = Annotated[str, Field(pattern=r"^[DAU][0-9]{2}$")]


class EvaluationProfileError(ValueError):
    """Reject configuration without reproducing arbitrary file contents or secret values."""


class ProfileModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", hide_input_in_errors=True)


class CaseSelection(ProfileModel):
    include: tuple[CaseId, ...] = ()
    exclude: tuple[CaseId, ...] = ()

    @field_validator("include", "exclude", mode="before")
    @classmethod
    def read_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_unique_selection(self) -> Self:
        if (
            len(set(self.include)) != len(self.include)
            or len(set(self.exclude)) != len(self.exclude)
            or set(self.include) & set(self.exclude)
        ):
            raise ValueError("case selections must be unique and disjoint")
        return self


class RetrievalSettings(ProfileModel):
    lexical_strategy: Literal["websearch", "weighted_or"] = "websearch"
    text_neighbors: bool = True
    page_expansion: bool = True
    company_priority: bool = True
    decompose_query: bool = False
    lexical_candidate_limit: int | None = Field(default=None, ge=1, le=20)
    dense_candidate_limit: int | None = Field(default=None, ge=1, le=20)
    chunk_limit: int | None = Field(default=None, ge=1, le=8)
    character_limit: int | None = Field(default=None, ge=1, le=24_000)


class GenerationSettings(ProfileModel):
    input_overhead_limit: int | None = Field(default=None, ge=16_000, le=18_000)


class RerankingSettings(ProfileModel):
    strategy: Literal["none", "luna", "pinecone"] = "none"


class ModelSettings(ProfileModel):
    answer: ModelName = "gpt-5.6-terra"
    evidence_check: ModelName = "gpt-5.6-terra"


class ArchiveSettings(ProfileModel):
    modes: tuple[Literal["keyword", "semantic", "hybrid"], ...] = ("keyword",)

    @field_validator("modes", mode="before")
    @classmethod
    def read_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_unique_modes(self) -> Self:
        if not self.modes or len(set(self.modes)) != len(self.modes):
            raise ValueError("archive modes must be nonempty and unique")
        return self


class InputSettings(ProfileModel):
    snapshot: str = ""

    @field_validator("snapshot")
    @classmethod
    def require_local_output_path(cls, value: str) -> str:
        if value:
            checked_repository_file(value, subtree="outputs", suffixes=(".json", ".jsonl"))
        return value


class BudgetSettings(ProfileModel):
    max_spend_usd: Annotated[str, Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")] = "0.00"
    max_provider_calls: int = Field(default=0, ge=0, le=1_000)

    @field_validator("max_spend_usd")
    @classmethod
    def retain_reviewed_run_ceiling(cls, value: str) -> str:
        if Decimal(value) > Decimal("0.80"):
            raise ValueError("the reviewed per-run policy caps spending at USD 0.80")
        return value


class EvaluationProfile(ProfileModel):
    schema_version: Literal[1] = 1
    name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
    catalog: str
    mode: Literal["offline_replay", "retrieval_live", "rag_live"] = "offline_replay"
    scope: Literal["whole_archive", "catalog_documents"] = "whole_archive"
    repetitions: int = Field(default=1, ge=1, le=10)
    cases: CaseSelection = Field(default_factory=CaseSelection)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    generation: GenerationSettings = Field(default_factory=GenerationSettings)
    reranking: RerankingSettings = Field(default_factory=RerankingSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    archive: ArchiveSettings = Field(default_factory=ArchiveSettings)
    inputs: InputSettings = Field(default_factory=InputSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("profile version must be an integer")
        return value

    @model_validator(mode="after")
    def reject_ignored_switches(self) -> Self:
        retrieval = self.retrieval
        if not retrieval.page_expansion and (
            retrieval.company_priority or self.reranking.strategy == "luna"
        ):
            raise ValueError("company prioritization and Luna require page expansion")
        if self.reranking.strategy == "pinecone" and any(
            (retrieval.page_expansion, retrieval.text_neighbors, retrieval.company_priority)
        ):
            raise ValueError("the BGE path does not apply page/neighbor/company expansion")
        spend = Decimal(self.budget.max_spend_usd)
        calls = self.budget.max_provider_calls
        if self.mode == "offline_replay" and (spend != 0 or calls != 0):
            raise ValueError("offline replay requires zero provider budget and calls")
        if self.mode == "retrieval_live" and self.inputs.snapshot:
            raise ValueError("fresh retrieval collection cannot use a replay snapshot")
        return self


def checked_repository_file(value: str, *, subtree: str, suffixes: tuple[str, ...]) -> Path:
    """Keep profile/catalog/snapshot references out of credentials and other directories."""

    path = Path(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or path.parts[0] != subtree
        or any(part.startswith(".") for part in path.parts)
        or path.suffix not in suffixes
    ):
        raise EvaluationProfileError("unsupported evaluation file reference")
    resolved = (REPOSITORY_ROOT / path).resolve()
    if not resolved.is_relative_to(REPOSITORY_ROOT.resolve() / subtree):
        raise EvaluationProfileError("evaluation file reference escapes its allowed directory")
    return resolved


def load_profile(path: Path) -> tuple[EvaluationProfile, bytes]:
    """Read only TOML profiles under eval/profiles, never environment or secret files."""

    resolved = path.resolve()
    if (
        resolved.suffix != ".toml"
        or resolved.name.startswith(".")
        or not resolved.is_relative_to(REPOSITORY_ROOT.resolve() / "eval/profiles")
    ):
        raise EvaluationProfileError("profile must be a TOML file under eval/profiles")
    try:
        content = resolved.read_bytes()
        return EvaluationProfile.model_validate(loads(content.decode("utf-8"))), content
    except OSError, UnicodeError, TOMLDecodeError, ValidationError:
        raise EvaluationProfileError(
            "evaluation profile is unreadable or has invalid settings"
        ) from None


def effective_rag_settings(profile: EvaluationProfile, base: RagSettings) -> RagSettings:
    """Apply only explicit overrides to the shared application's validated bounds."""

    values = base.model_dump()
    for name in ("lexical_candidate_limit", "dense_candidate_limit"):
        if (value := getattr(profile.retrieval, name)) is not None:
            values["retrieval"][name] = value
    for name in ("chunk_limit", "character_limit"):
        if (value := getattr(profile.retrieval, name)) is not None:
            values["context"][name] = value
    if profile.generation.input_overhead_limit is not None:
        values["generation"]["input_overhead_limit"] = profile.generation.input_overhead_limit
    result = RagSettings.model_validate(values)
    if result.context.chunk_limit > 8:
        raise EvaluationProfileError("source-ID evaluation requires at most eight context chunks")
    return result
