"""Compare individual and batched evidence judgments on fixed synthetic inputs."""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256
from time import monotonic
from uuid import NAMESPACE_URL, uuid5

from investment_research.infrastructure.evidence.openai import (
    M1_5_DOCUMENT_ATTRIBUTION_BATCH_VERSION,
    M1_5_LOCAL_DOCUMENT_ATTRIBUTION_SUPPORT_REASON_PROMPT_VERSION,
    OPENAI_STATEMENT_SUPPORT_ACCOUNTING_INPUT_OVERHEAD_UNITS,
    OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
    create_openai_statement_support_evaluator,
)
from investment_research.infrastructure.openai_models import RESEARCH_MODEL_PRICES
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.evidence.contracts import (
    M1_5_DOCUMENT_ATTRIBUTION_POLICY_VERSION,
    ContextualStatementEvidence,
    ContextualStatementSupportInput,
    OriginalSupportContext,
    StatementSupportJudgment,
    ValidatedEvidenceReference,
)
from investment_research.modules.evidence.ports import (
    BatchContextualStatementSupportEvaluator,
    ContextualSupportBatch,
    StatementSupportProviderError,
)
from investment_research.modules.evidence.support_context import original_attribution_prefix_range
from investment_research.modules.operations.application import ProviderCostPolicy
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.research.contracts import ResearchModel
from scripts.m1_5_live_settings import load_evaluation_generation_key
from scripts.plan_m1_5_live_baseline import (
    APPROVED_CLOSEOUT_V2_SHA256,
    CLOSEOUT_V2_MANIFEST,
    _budget_argument,
)
from scripts.verify_m1_5_corpus import (
    REPOSITORY_ROOT,
    CorpusValidationError,
    _fixture_bytes,
    validate_corpus,
)


@dataclass(frozen=True, slots=True)
class Claim:
    id: str
    text: str
    passage_ids: tuple[str, ...]
    expected_supported: bool


TRANSFORMERS = "passage:fy2026-transformer-constraint"
SOFTWARE = "passage:fy2026-software-constraint"
BUDGET = "passage:fy2026-european-pilot-budget-second"
LEAD_TIMES = "passage:fy2026-supplier-lead-time-expectation"
CLAIM_GROUPS = (
    (
        Claim("pilot-budget", "The European pilot budget is €12 million.", (BUDGET,), True),
        Claim(
            "budget-wrong-citation",
            "The European pilot budget is €12 million.",
            (TRANSFORMERS,),
            False,
        ),
    ),
    (
        Claim(
            "operations-note-attribution",
            "The Operations Note identifies software configuration capacity as the primary "
            "constraint on new customer deployments.",
            (SOFTWARE,),
            True,
        ),
        Claim(
            "wrong-report-attribution",
            "The Capacity Update identifies software configuration capacity as the primary "
            "constraint on new customer deployments.",
            (SOFTWARE,),
            False,
        ),
        Claim(
            "capacity-update-attribution",
            "The Capacity Update identifies a shortage of high-voltage transformers "
            "as the primary constraint on new customer deployments.",
            (TRANSFORMERS,),
            True,
        ),
    ),
    (
        Claim(
            "reports-disagree",
            "The Capacity Update and Operations Note disagree about the primary constraint "
            "on new customer deployments.",
            (TRANSFORMERS, SOFTWARE),
            True,
        ),
        Claim(
            "missing-qualification",
            "Supplier lead times are guaranteed to improve in the first half of 2027.",
            (LEAD_TIMES,),
            False,
        ),
    ),
)


def prepare_batches() -> tuple[ContextualSupportBatch, ...]:
    """Load exact quotes and original text prefixes from the approved synthetic catalog."""

    if sha256(CLOSEOUT_V2_MANIFEST.read_bytes()).hexdigest() != APPROVED_CLOSEOUT_V2_SHA256:
        raise ValueError("the synthetic catalog version is not approved")
    manifest = validate_corpus(CLOSEOUT_V2_MANIFEST, REPOSITORY_ROOT)
    documents = {document.id: document for document in manifest.documents}
    passages = {passage.id: passage for passage in manifest.passages}
    evidence: dict[str, ContextualStatementEvidence] = {}
    for passage_id in dict.fromkeys(
        passage_id for group in CLAIM_GROUPS for claim in group for passage_id in claim.passage_ids
    ):
        passage = passages[passage_id]
        document = documents[passage.document_id]
        if document.media_type != "text/plain" or passage.source_anchor.kind != "TEXT_PASSAGE":
            raise ValueError("the comparison requires a synthetic text passage")
        original = (
            _fixture_bytes(REPOSITORY_ROOT, document.path, document.sha256)
            .decode("utf-8-sig")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
        start, end = original_attribution_prefix_range(original)
        document_id = uuid5(NAMESPACE_URL, document.id)
        version_id = uuid5(document_id, document.sha256)
        evidence[passage_id] = ContextualStatementEvidence(
            reference=ValidatedEvidenceReference(
                retrieval_candidate_id=uuid5(version_id, passage_id),
                document_id=document_id,
                document_version_id=version_id,
                structure_block_id=uuid5(version_id, f"block:{passage_id}"),
                source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
                source_start_character=passage.source_anchor.start_character,
                source_end_character=passage.source_anchor.end_character,
                start_character=0,
                end_character=len(passage.text),
                exact_text=passage.text,
                text_sha256=sha256(passage.text.encode()).hexdigest(),
            ),
            attribution_context=OriginalSupportContext(
                policy_version=M1_5_DOCUMENT_ATTRIBUTION_POLICY_VERSION,
                workspace_id=uuid5(NAMESPACE_URL, manifest.id),
                processing_run_id=uuid5(version_id, "benchmark-processing-run"),
                document_id=document_id,
                document_version_id=version_id,
                structure_block_id=uuid5(version_id, "first-block"),
                start_character=start,
                end_character=end,
                text=original[start:end],
            ),
        )
    return tuple(
        {
            ordinal: ContextualStatementSupportInput(
                statement=claim.text, evidence=tuple(evidence[key] for key in claim.passage_ids)
            )
            for ordinal, claim in enumerate(group)
        }
        for group in CLAIM_GROUPS
    )


def _maximum_usage(requests: ContextualSupportBatch, *, batch: bool) -> ProviderUsage:
    # The canonical dumps include more fields than the provider payload. Reserve additional
    # bytes for the batch envelope and ordinals.
    envelope = 16 + 32 * len(requests) if batch else 0
    return ProviderUsage(
        input_units=OPENAI_STATEMENT_SUPPORT_ACCOUNTING_INPUT_OVERHEAD_UNITS
        + envelope
        + sum(len(request.model_dump_json().encode()) for request in requests.values()),
        output_units=OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS * len(requests),
    )


def _maximum_cost(batches: tuple[ContextualSupportBatch, ...], prices: ProviderCostPolicy) -> int:
    return sum(
        prices.cost_for(_maximum_usage(requests, batch=True))
        + sum(
            prices.cost_for(_maximum_usage({ordinal: request}, batch=False))
            for ordinal, request in requests.items()
        )
        for requests in batches
    )


@dataclass(slots=True)
class Measurements:
    calls: int = 0
    elapsed_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_micro_usd: int = 0
    correct_judgments: int = 0

    def record(
        self,
        usage: ProviderUsage,
        maximum: ProviderUsage,
        elapsed: float,
        prices: ProviderCostPolicy,
    ) -> None:
        if usage.input_units > maximum.input_units or usage.output_units > maximum.output_units:
            raise ValueError("the provider reported usage above the request reservation")
        self.calls += 1
        self.elapsed_ms += round(1_000 * elapsed)
        self.input_tokens += usage.input_units
        self.output_tokens += usage.output_units
        self.estimated_cost_micro_usd += prices.cost_for(usage)


def compare(
    batches: tuple[ContextualSupportBatch, ...],
    evaluator: BatchContextualStatementSupportEvaluator,
    prices: ProviderCostPolicy,
    *,
    clock: Callable[[], float] = monotonic,
) -> dict[str, object]:
    individual = Measurements()
    batched = Measurements()
    comparisons: list[dict[str, object]] = []
    for group, requests in zip(CLAIM_GROUPS, batches, strict=True):
        single_judgments: dict[int, StatementSupportJudgment] = {}
        for ordinal, request in requests.items():
            started = clock()
            result = evaluator.evaluate_contextual(request)
            individual.record(
                result.usage,
                _maximum_usage({ordinal: request}, batch=False),
                clock() - started,
                prices,
            )
            single_judgments[ordinal] = result

        started = clock()
        batch = evaluator.evaluate_contextual_batch(requests)
        batched.record(batch.usage, _maximum_usage(requests, batch=True), clock() - started, prices)
        if batch.judgments.keys() != requests.keys():
            raise ValueError("the batch judgments do not match the frozen inputs")
        for ordinal, claim in enumerate(group):
            single = single_judgments[ordinal].decision.supported
            combined = batch.judgments[ordinal].decision.supported
            individual.correct_judgments += single == claim.expected_supported
            batched.correct_judgments += combined == claim.expected_supported
            comparisons.append(
                {
                    "id": claim.id,
                    "expected_supported": claim.expected_supported,
                    "individual_supported": single,
                    "batch_supported": combined,
                }
            )
    return {
        "individual": asdict(individual),
        "batch": asdict(batched),
        "judgments": comparisons,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=tuple(ResearchModel), default=ResearchModel.LUNA)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-public-synthetic", action="store_true")
    parser.add_argument("--max-spend-usd", type=_budget_argument)
    args = parser.parse_args(argv)
    if args.execute and (not args.confirm_public_synthetic or args.max_spend_usd is None):
        parser.error("--execute requires --confirm-public-synthetic and --max-spend-usd")

    try:
        batches = prepare_batches()
        model = ResearchModel(args.model)
        price = RESEARCH_MODEL_PRICES[model]
        prices = ProviderCostPolicy(
            price.input_micro_usd_per_million_tokens, price.output_micro_usd_per_million_tokens
        )
        reserved = _maximum_cost(batches, prices)
        report: dict[str, object] = {
            "model": model,
            "corpus_sha256": APPROVED_CLOSEOUT_V2_SHA256,
            "individual_config": M1_5_LOCAL_DOCUMENT_ATTRIBUTION_SUPPORT_REASON_PROMPT_VERSION,
            "batch_config": M1_5_DOCUMENT_ATTRIBUTION_BATCH_VERSION,
            "statements": sum(map(len, batches)),
            "individual_calls": sum(map(len, batches)),
            "batch_calls": len(batches),
            "reserved_cost_micro_usd": reserved,
        }
        if args.execute:
            if reserved > args.max_spend_usd:
                print("The estimated reservation exceeds the requested budget.", file=sys.stderr)
                return 2
            evaluator = create_openai_statement_support_evaluator(
                api_key=load_evaluation_generation_key().get_secret_value(),
                model=model.value,
                include_decision_reason=True,
                include_attribution_context=True,
                include_document_attribution_context=True,
            )
            try:
                report.update(compare(batches, evaluator, prices))
            finally:
                evaluator.close()
        print(json.dumps(report, indent=2))
        return 0
    except OSError, ValueError, CorpusValidationError, StatementSupportProviderError:
        print("The evidence support comparison failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
