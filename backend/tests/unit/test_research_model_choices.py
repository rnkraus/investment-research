"""Offline checks for the allowlisted research-model contract."""

import pytest
from pydantic import ValidationError

from investment_research.infrastructure.openai_models import (
    RESEARCH_MODEL_PRICES,
    answer_config_version,
    support_config_version,
)
from investment_research.modules.research.contracts import ResearchModel, ResearchRetrievalInput


def test_default_model_pair_preserves_terra_behavior() -> None:
    request = ResearchRetrievalInput(question="What does the source say?")

    assert request.generation_model is ResearchModel.TERRA
    assert request.support_model is ResearchModel.TERRA


@pytest.mark.parametrize("field", ["generation_model", "support_model"])
def test_unknown_research_model_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        ResearchRetrievalInput.model_validate(
            {"question": "What does the source say?", field: "unapproved-model"}
        )


def test_approved_models_have_distinct_prices_and_configuration_versions() -> None:
    assert set(RESEARCH_MODEL_PRICES) == set(ResearchModel)
    assert RESEARCH_MODEL_PRICES[ResearchModel.LUNA].input_micro_usd_per_million_tokens == 200_000
    assert (
        RESEARCH_MODEL_PRICES[ResearchModel.LUNA].output_micro_usd_per_million_tokens == 1_200_000
    )
    assert answer_config_version(ResearchModel.TERRA) != answer_config_version(ResearchModel.LUNA)
    assert support_config_version(ResearchModel.TERRA) != support_config_version(ResearchModel.LUNA)
