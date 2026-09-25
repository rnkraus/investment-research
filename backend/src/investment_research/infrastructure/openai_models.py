"""Versioned, allowlisted OpenAI research-model prices and identifiers."""

from dataclasses import dataclass
from types import MappingProxyType

from investment_research.modules.research.contracts import ResearchModel


@dataclass(frozen=True, slots=True)
class ResearchModelPrice:
    """Standard short-context USD prices in micro-dollars per million tokens."""

    input_micro_usd_per_million_tokens: int
    output_micro_usd_per_million_tokens: int


RESEARCH_MODEL_PRICES = MappingProxyType(
    {
        ResearchModel.TERRA: ResearchModelPrice(2_000_000, 12_000_000),
        ResearchModel.LUNA: ResearchModelPrice(200_000, 1_200_000),
    }
)


def answer_config_version(model: ResearchModel) -> str:
    """Give each answer model an independently traceable configuration version."""

    if model is ResearchModel.TERRA:
        return "openai-gpt-5.6-terra-low-answer-v2"
    return "openai-gpt-5.6-luna-low-answer-v1"


def support_config_version(model: ResearchModel) -> str:
    """Give each support model an independently traceable configuration version."""

    if model is ResearchModel.TERRA:
        return "openai-gpt-5.6-terra-low-support-v1"
    return "openai-gpt-5.6-luna-low-support-v1"
