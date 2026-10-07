"""Print a credential-free M1.5 live plan and provide its per-invocation budget guard."""

import argparse
import hashlib
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from investment_research.infrastructure.evidence.openai import (
    LOCAL_STATEMENT_SUPPORT_REASON_PROMPT_VERSION,
    M1_STATEMENT_SUPPORT_PROMPT_VERSION,
    OPENAI_STATEMENT_SUPPORT_ACCOUNTING_INPUT_OVERHEAD_UNITS,
    OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
)
from investment_research.infrastructure.generation.openai import (
    M1_ANSWER_GENERATOR_VERSION,
    M1_ANSWER_PROMPT_VERSION,
    OPENAI_ANSWER_ACCOUNTING_INPUT_OVERHEAD_UNITS,
    OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
)
from investment_research.infrastructure.generation.openai_citable_units import (
    M1_5_CITABLE_ANSWER_PROMPT_VERSION,
    citable_answer_profile,
)
from investment_research.infrastructure.openai_models import (
    RESEARCH_MODEL_PRICES,
    answer_config_version,
)
from investment_research.infrastructure.search.openai import (
    OPENAI_EMBEDDING_INPUT_MICRO_USD_PER_MILLION_TOKENS,
)
from investment_research.modules.operations.domain import ProviderOperationKind, ProviderUsage
from investment_research.modules.processing.application import M1_PROCESSING_PROFILE
from investment_research.modules.research.contracts import (
    M1_GENERATED_STATEMENT_LIMIT,
    AnswerGenerationProfile,
    ResearchModel,
)
from investment_research.modules.search.contracts import (
    M1_SELECTOR_CONFIG_VERSION,
    M1SelectionProfile,
)
from investment_research.modules.search.fusion_contracts import (
    RRF_CONFIG_VERSION,
    RrfSelectionProfile,
)
from scripts.score_m1_5_observations import RunConfiguration
from scripts.verify_m1_5_corpus import (
    DEFAULT_MANIFEST,
    REPOSITORY_ROOT,
    CorpusValidationError,
    validate_corpus,
)

APPROVED_CORPUS_SHA256 = "ee526a2b708161e0c017b287529a62820978093e479fc78f2cea75a96a41d5f9"
MAXIMUM_SPEND_MICRO_USD = 800_000
MAXIMUM_DOCUMENT_VECTORS = 64
MAXIMUM_INPUT_UNITS_PER_REQUEST = 128_000
type LiveRetrievalPolicy = Literal["m1", "rrf"]
type LiveAnswerPolicy = Literal["m1", "citable_units"]


class LiveBaselineLimitError(ValueError):
    """Reject a plan or request without including provider or corpus content."""


def answer_profile_for_policy(
    policy: LiveAnswerPolicy, model: ResearchModel
) -> AnswerGenerationProfile:
    """Freeze the answer contract independently of the retrieval selector."""

    if policy == "citable_units":
        return citable_answer_profile(model)
    if policy == "m1":
        return AnswerGenerationProfile(
            generator_version=M1_ANSWER_GENERATOR_VERSION,
            generation_config_version=answer_config_version(model),
            prompt_version=M1_ANSWER_PROMPT_VERSION,
        )
    raise LiveBaselineLimitError("unknown answer policy")


def answer_policy_for_prompt_version(version: str) -> LiveAnswerPolicy:
    """Never silently select another generator for an unknown recorded version."""

    if version == M1_ANSWER_PROMPT_VERSION:
        return "m1"
    if version == M1_5_CITABLE_ANSWER_PROMPT_VERSION:
        return "citable_units"
    raise LiveBaselineLimitError("unknown answer prompt version")


def selection_profile_for_policy(
    policy: LiveRetrievalPolicy,
) -> M1SelectionProfile | RrfSelectionProfile:
    """Allow only the two frozen experimental profiles, without changing runtime defaults."""

    if policy == "m1":
        return M1SelectionProfile()
    if policy == "rrf":
        return RrfSelectionProfile()
    raise LiveBaselineLimitError("the evaluation retrieval policy is not approved")


def retrieval_policy_for_version(version: str) -> LiveRetrievalPolicy:
    """Resolve a recorded configuration explicitly; never silently fall back to M1."""

    if version == M1_SELECTOR_CONFIG_VERSION:
        return "m1"
    if version == RRF_CONFIG_VERSION:
        return "rrf"
    raise LiveBaselineLimitError("the evaluation selector version is not approved")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)


class RoleLimit(_FrozenModel):
    role: ProviderOperationKind
    maximum_requests: int = Field(gt=0)
    input_envelope_units: int = Field(ge=0)
    maximum_output_tokens: int = Field(ge=0)
    input_micro_usd_per_million_tokens: int = Field(ge=0)
    output_micro_usd_per_million_tokens: int = Field(ge=0)


class LiveBaselinePlan(_FrozenModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["m1.5-live-budget-v1"] = "m1.5-live-budget-v1"
    execution_mode: Literal["preflight_only"] = "preflight_only"
    corpus_id: str
    corpus_sha256: str
    document_count: int
    case_ids: tuple[str, ...]
    configuration: RunConfiguration
    maximum_spend_micro_usd: int = Field(gt=0, le=MAXIMUM_SPEND_MICRO_USD)
    maximum_document_vectors: int = MAXIMUM_DOCUMENT_VECTORS
    maximum_input_units_per_request: int = MAXIMUM_INPUT_UNITS_PER_REQUEST
    role_limits: tuple[RoleLimit, ...]
    price_basis: Literal["repository_versioned_estimates"] = "repository_versioned_estimates"
    pinecone_billing: Literal["starter_only_not_in_openai_estimate"] = (
        "starter_only_not_in_openai_estimate"
    )


def build_plan(
    *,
    answer_model: ResearchModel = ResearchModel.TERRA,
    support_model: ResearchModel = ResearchModel.TERRA,
    maximum_spend_micro_usd: int = MAXIMUM_SPEND_MICRO_USD,
    retrieval_policy: LiveRetrievalPolicy = "m1",
    answer_policy: LiveAnswerPolicy = "m1",
    support_explanations: bool = False,
) -> LiveBaselinePlan:
    """Validate only the frozen approved fixtures; never load runtime settings or keys."""

    selection_profile = selection_profile_for_policy(retrieval_policy)
    if type(maximum_spend_micro_usd) is not int or not (
        0 < maximum_spend_micro_usd <= MAXIMUM_SPEND_MICRO_USD
    ):
        raise LiveBaselineLimitError("the invocation budget must be positive and at most USD 0.80")
    try:
        digest = hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest()
        if digest != APPROVED_CORPUS_SHA256:
            raise LiveBaselineLimitError("the corpus version is not approved for this live plan")
        manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    except OSError:
        raise LiveBaselineLimitError("the approved corpus is unavailable") from None
    if manifest.review_status != "reviewed":
        raise LiveBaselineLimitError("the corpus awaits owner review")
    limits: list[RoleLimit] = []
    for role in ProviderOperationKind:
        if role in {
            ProviderOperationKind.DOCUMENT_EMBEDDING,
            ProviderOperationKind.QUERY_EMBEDDING,
        }:
            requests = (
                MAXIMUM_DOCUMENT_VECTORS
                if role is ProviderOperationKind.DOCUMENT_EMBEDDING
                else len(manifest.cases)
            )
            envelope = output = output_price = 0
            input_price = OPENAI_EMBEDDING_INPUT_MICRO_USD_PER_MILLION_TOKENS
        else:
            answer = role is ProviderOperationKind.ANSWER_GENERATION
            model_price = RESEARCH_MODEL_PRICES[answer_model if answer else support_model]
            requests = len(manifest.cases) * (1 if answer else M1_GENERATED_STATEMENT_LIMIT)
            envelope = (
                OPENAI_ANSWER_ACCOUNTING_INPUT_OVERHEAD_UNITS
                if answer
                else OPENAI_STATEMENT_SUPPORT_ACCOUNTING_INPUT_OVERHEAD_UNITS
            )
            output = (
                OPENAI_ANSWER_MAX_OUTPUT_TOKENS
                if answer
                else OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS
            )
            input_price = model_price.input_micro_usd_per_million_tokens
            output_price = model_price.output_micro_usd_per_million_tokens
        limits.append(
            RoleLimit(
                role=role,
                maximum_requests=requests,
                input_envelope_units=envelope,
                maximum_output_tokens=output,
                input_micro_usd_per_million_tokens=input_price,
                output_micro_usd_per_million_tokens=output_price,
            )
        )
    return LiveBaselinePlan(
        corpus_id=manifest.id,
        corpus_sha256=digest,
        document_count=len(manifest.documents),
        case_ids=tuple(case.id for case in manifest.cases),
        configuration=RunConfiguration(
            lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
            embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
            dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
            selector_config_version=selection_profile.selector_config_version,
            answer_model=answer_model.value,
            support_model=support_model.value,
            answer_prompt_version=answer_profile_for_policy(
                answer_policy, answer_model
            ).prompt_version,
            support_prompt_version=(
                LOCAL_STATEMENT_SUPPORT_REASON_PROMPT_VERSION
                if support_explanations
                else M1_STATEMENT_SUPPORT_PROMPT_VERSION
            ),
        ),
        maximum_spend_micro_usd=maximum_spend_micro_usd,
        role_limits=tuple(limits),
    )


class RequestReservation(_FrozenModel):
    ordinal: int
    role: ProviderOperationKind
    input_units: int
    output_units: int
    estimated_cost_micro_usd: int


@dataclass(slots=True)
class LiveBaselineBudget:
    """Guard sequential evaluation calls, retaining every reservation even after failure."""

    plan: LiveBaselinePlan
    _reservations: list[RequestReservation] = field(default_factory=list, init=False)
    _halted: bool = field(default=False, init=False)

    @property
    def reserved_cost_micro_usd(self) -> int:
        return sum(item.estimated_cost_micro_usd for item in self._reservations)

    def reserve(
        self, role: ProviderOperationKind, *, payload_utf8_bytes: int
    ) -> RequestReservation:
        """Reserve UTF-8 payload bytes plus adapter envelope and its full output limit."""

        if self._halted:
            raise LiveBaselineLimitError("the invocation stopped after a usage contract failure")
        if type(payload_utf8_bytes) is not int or payload_utf8_bytes <= 0:
            raise LiveBaselineLimitError("request payload size must be a positive integer")
        limit = next((item for item in self.plan.role_limits if item.role is role), None)
        if limit is None:
            raise LiveBaselineLimitError("the provider role is not part of this plan")
        if sum(item.role is role for item in self._reservations) >= limit.maximum_requests:
            raise LiveBaselineLimitError("the provider role request limit is exhausted")
        input_units = payload_utf8_bytes + limit.input_envelope_units
        if input_units > self.plan.maximum_input_units_per_request:
            raise LiveBaselineLimitError("the request input limit is exceeded")
        numerator = (
            input_units * limit.input_micro_usd_per_million_tokens
            + limit.maximum_output_tokens * limit.output_micro_usd_per_million_tokens
        )
        cost = (numerator + 999_999) // 1_000_000
        if self.reserved_cost_micro_usd + cost > self.plan.maximum_spend_micro_usd:
            raise LiveBaselineLimitError("the invocation OpenAI budget is exhausted")
        reservation = RequestReservation(
            ordinal=len(self._reservations),
            role=role,
            input_units=input_units,
            output_units=limit.maximum_output_tokens,
            estimated_cost_micro_usd=cost,
        )
        self._reservations.append(reservation)
        return reservation

    def verify_usage(self, reservation: RequestReservation, usage: ProviderUsage) -> None:
        """Stop on unknown reservations or usage above the pre-call accounting bound."""

        if (
            not 0 <= reservation.ordinal < len(self._reservations)
            or self._reservations[reservation.ordinal] is not reservation
            or usage.input_units > reservation.input_units
            or usage.output_units > reservation.output_units
        ):
            self._halted = True
            raise LiveBaselineLimitError("provider usage does not fit the request reservation")


def _budget_argument(value: str) -> int:
    try:
        dollars = Decimal(value)
        maximum_dollars = Decimal(MAXIMUM_SPEND_MICRO_USD).scaleb(-6)
        if not dollars.is_finite() or not Decimal("0.000001") <= dollars <= maximum_dollars:
            raise ValueError
        numerator, denominator = dollars.as_integer_ratio()
        amount, remainder = divmod(numerator * 1_000_000, denominator)
        if remainder:
            raise ValueError
        return amount
    except InvalidOperation, ValueError:
        raise argparse.ArgumentTypeError(
            "use a positive USD amount at most 0.80, in micro-dollar steps"
        ) from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrieval-policy", choices=("m1", "rrf"), default="m1")
    parser.add_argument("--answer-policy", choices=("m1", "citable_units"), default="m1")
    parser.add_argument(
        "--answer-model",
        type=ResearchModel,
        choices=list(ResearchModel),
        default=ResearchModel.TERRA,
    )
    parser.add_argument(
        "--support-model",
        type=ResearchModel,
        choices=list(ResearchModel),
        default=ResearchModel.TERRA,
    )
    parser.add_argument("--max-spend-usd", type=_budget_argument, default=MAXIMUM_SPEND_MICRO_USD)
    arguments = parser.parse_args()
    try:
        plan = build_plan(
            answer_model=arguments.answer_model,
            support_model=arguments.support_model,
            maximum_spend_micro_usd=arguments.max_spend_usd,
            retrieval_policy=arguments.retrieval_policy,
            answer_policy=arguments.answer_policy,
        )
    except CorpusValidationError, LiveBaselineLimitError:
        print(
            "M1.5 live preflight failed; corpus and provider details were withheld.",
            file=sys.stderr,
        )
        return 1
    print(plan.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
