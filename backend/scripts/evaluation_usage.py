"""Content-free provider usage totals; reservations never stand in for unknown token usage."""

from collections.abc import Mapping, Sequence
from typing import Self

from pydantic import Field, model_validator

from scripts.evaluation_profiles import CaseId, ProfileModel
from scripts.portfolio_snapshots import ArchiveMode

COUNTS = (
    "attempts",
    "reported_attempts",
    "unknown_attempts",
    "unmetered_attempts",
    "failed_attempts",
    "input_tokens",
    "output_tokens",
    "estimated_usage_cost_micro_usd",
    "reserved_micro_usd",
)


class RoleUsage(ProfileModel):
    role: str = Field(pattern=r"^[A-Za-z_]{1,64}$")
    provider: str = Field(pattern=r"^[a-z0-9_.-]{1,64}$")
    model: str | None = Field(default=None, max_length=200)
    attempts: int = Field(ge=1)
    reported_attempts: int = Field(ge=0)
    unknown_attempts: int = Field(ge=0)
    unmetered_attempts: int = Field(ge=0)
    failed_attempts: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_usage_cost_micro_usd: int = Field(ge=0)
    reserved_micro_usd: int = Field(ge=0)

    @model_validator(mode="after")
    def require_consistent_counts(self) -> Self:
        if (
            self.reported_attempts + self.unknown_attempts + self.unmetered_attempts
            != self.attempts
        ):
            raise ValueError("usage completeness counts must account for every attempt")
        if self.failed_attempts > self.attempts or (
            self.reported_attempts == 0
            and (self.input_tokens or self.output_tokens or self.estimated_usage_cost_micro_usd)
        ):
            raise ValueError("unknown usage cannot supply token or estimated usage totals")
        return self

    @property
    def key(self) -> tuple[str, str, str | None]:
        return self.role, self.provider, self.model


class UsageSummary(ProfileModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    groups: tuple[RoleUsage, ...] = Field(default=(), max_length=1_000)

    @model_validator(mode="after")
    def require_unique_groups(self) -> Self:
        if len({item.key for item in self.groups}) != len(self.groups):
            raise ValueError("provider usage groups must be unique")
        return self

    def total(self, name: str) -> int:
        if name not in COUNTS:
            raise ValueError("unknown usage total")
        return sum(getattr(item, name) for item in self.groups)


class CaseUsage(ProfileModel):
    case: CaseId
    repetition: int = Field(ge=1, le=10)
    archive_mode: ArchiveMode | None = None
    usage: UsageSummary

    @property
    def key(self) -> tuple[str, ArchiveMode | None, int]:
        return self.case, self.archive_mode, self.repetition


def summarize_attempts(attempts: Sequence[Mapping[str, object]]) -> UsageSummary:
    groups: dict[tuple[str, str, str | None], dict[str, int]] = {}
    for attempt in attempts:
        model = attempt.get("model")
        key = (
            str(attempt["role"]),
            str(attempt["provider"]),
            model if isinstance(model, str) else None,
        )
        counts = groups.setdefault(key, dict.fromkeys(COUNTS, 0))
        counts["attempts"] += 1
        counts["failed_attempts"] += attempt.get("state") == "FAILED"
        status = attempt.get("usage_status", "unknown")
        counts[
            {"reported": "reported_attempts", "not_applicable": "unmetered_attempts"}.get(
                str(status), "unknown_attempts"
            )
        ] += 1
        for source, destination in (
            ("reserved_micro_usd", "reserved_micro_usd"),
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("estimated_usage_cost_micro_usd", "estimated_usage_cost_micro_usd"),
        ):
            value = attempt.get(source)
            if isinstance(value, int):
                counts[destination] += value
    return UsageSummary(
        groups=tuple(
            RoleUsage.model_validate(
                {"role": key[0], "provider": key[1], "model": key[2], **counts}
            )
            for key, counts in groups.items()
        )
    )


def merge_usage(summaries: Sequence[UsageSummary]) -> UsageSummary:
    groups: dict[tuple[str, str, str | None], dict[str, int]] = {}
    for summary in summaries:
        for group in summary.groups:
            counts = groups.setdefault(group.key, dict.fromkeys(COUNTS, 0))
            for name in COUNTS:
                counts[name] += getattr(group, name)
    return UsageSummary(
        groups=tuple(
            RoleUsage.model_validate(
                {"role": key[0], "provider": key[1], "model": key[2], **counts}
            )
            for key, counts in groups.items()
        )
    )
