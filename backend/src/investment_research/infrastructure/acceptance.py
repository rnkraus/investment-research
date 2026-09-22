"""Frozen synthetic providers for the credential-free M1 acceptance runtime."""

from investment_research.infrastructure.evidence.deterministic import (
    DeterministicFixtureStatementSupportEvaluator,
    FixtureStatementSupportRule,
)
from investment_research.infrastructure.generation.deterministic import (
    M0_0_FIXTURE_ANSWER_CASE,
    M0_0_INSUFFICIENT_MESSAGE,
    DeterministicFixtureSetAnswerGenerator,
    FixtureAnswerCase,
)
from investment_research.infrastructure.search.deterministic import (
    DeterministicEmbeddingRule,
    DeterministicRuleEmbeddingProvider,
    DeterministicTextMatch,
)
from investment_research.modules.processing.application import M1_PROCESSING_PROFILE
from investment_research.modules.research.contracts import AnswerGenerationProfile
from investment_research.modules.search.contracts import EmbeddingPurpose

M1_ACCEPTANCE_GENERATION_PROFILE = AnswerGenerationProfile(
    generator_version="deterministic-m1-acceptance-v1",
    generation_config_version="deterministic-m1-acceptance-generation-v1",
    prompt_version="deterministic-m1-acceptance-prompt-v1",
)
M1_ACCEPTANCE_SUPPORT_CONFIG_VERSION = "deterministic-m1-acceptance-support-v1"
M1_TEXT_FIXTURE_ANSWER_CASE = FixtureAnswerCase(
    question="What is mainly holding back Northstar's rollout to new clients?",
    statement=(
        "Northstar identified a shortage of high-voltage transformers as the primary constraint "
        "on new customer deployments."
    ),
    evidence_text=(
        "The primary constraint on new customer deployments is a shortage of high-voltage "
        "transformers."
    ),
)


def create_m1_acceptance_embedding_provider() -> DeterministicRuleEmbeddingProvider:
    """Return the frozen semantic alias plus deterministic unrelated-input fallbacks."""

    return DeterministicRuleEmbeddingProvider(
        embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
        rules=(
            DeterministicEmbeddingRule(
                purpose=EmbeddingPurpose.QUERY,
                text=M1_TEXT_FIXTURE_ANSWER_CASE.question,
                match=DeterministicTextMatch.EXACT,
                dimension=0,
            ),
            DeterministicEmbeddingRule(
                purpose=EmbeddingPurpose.DOCUMENT,
                text=M1_TEXT_FIXTURE_ANSWER_CASE.evidence_text,
                match=DeterministicTextMatch.CONTAINS,
                dimension=0,
            ),
        ),
    )


def create_m1_acceptance_answer_generator() -> DeterministicFixtureSetAnswerGenerator:
    """Return the two answerable frozen cases and explicit insufficient behavior."""

    return DeterministicFixtureSetAnswerGenerator(
        answerable_cases=(M0_0_FIXTURE_ANSWER_CASE, M1_TEXT_FIXTURE_ANSWER_CASE),
        insufficient_message=M0_0_INSUFFICIENT_MESSAGE,
    )


def create_m1_acceptance_support_evaluator() -> DeterministicFixtureStatementSupportEvaluator:
    """Return exact support rules for both frozen accepted statements."""

    return DeterministicFixtureStatementSupportEvaluator(
        tuple(
            FixtureStatementSupportRule(
                statement=case.statement,
                evidence_text=case.evidence_text,
            )
            for case in (M0_0_FIXTURE_ANSWER_CASE, M1_TEXT_FIXTURE_ANSWER_CASE)
        )
    )
