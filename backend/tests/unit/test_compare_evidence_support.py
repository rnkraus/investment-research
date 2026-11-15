"""Check paired inputs, scoring, and paid-run admission without provider traffic."""

import json
from collections.abc import Sequence

import pytest

from investment_research.modules.evidence.contracts import (
    ContextualStatementSupportInput,
    StatementSupportBatchResult,
    StatementSupportDecision,
    StatementSupportJudgment,
    StatementSupportResult,
)
from investment_research.modules.evidence.ports import ContextualSupportBatch
from investment_research.modules.operations.application import ProviderCostPolicy
from investment_research.modules.operations.domain import ProviderUsage
from scripts import compare_evidence_support as benchmark


def test_comparison_uses_same_evidence_and_scores_each_judgment() -> None:
    batches = benchmark.prepare_batches()
    assert batches == benchmark.prepare_batches()
    expected = {
        request: claim.expected_supported
        for requests, group in zip(batches, benchmark.CLAIM_GROUPS, strict=True)
        for request, claim in zip(requests.values(), group, strict=True)
    }
    individual_calls: list[ContextualStatementSupportInput] = []
    batch_calls: list[ContextualSupportBatch] = []

    class Evaluator:
        def evaluate(self, statement: str, evidence_texts: Sequence[str]) -> StatementSupportResult:
            raise AssertionError("the benchmark requires contextual attribution")

        def evaluate_contextual(
            self, request: ContextualStatementSupportInput
        ) -> StatementSupportResult:
            individual_calls.append(request)
            return StatementSupportResult(
                decision=StatementSupportDecision(supported=expected[request]),
                usage=ProviderUsage(input_units=20, output_units=5),
            )

        def evaluate_contextual_batch(
            self, requests: ContextualSupportBatch
        ) -> StatementSupportBatchResult:
            batch_calls.append(requests)
            return StatementSupportBatchResult(
                judgments={
                    ordinal: StatementSupportJudgment(
                        decision=StatementSupportDecision(
                            supported=expected[request] or request == batches[0][1]
                        )
                    )
                    for ordinal, request in requests.items()
                },
                usage=ProviderUsage(input_units=35, output_units=10),
            )

    report = benchmark.compare(
        batches, Evaluator(), ProviderCostPolicy(200_000, 1_200_000), clock=lambda: 0.0
    )
    assert len(individual_calls) == 7 and len(batch_calls) == 3
    assert individual_calls == [request for group in batch_calls for request in group.values()]
    assert report["individual"] == {
        "calls": 7,
        "elapsed_ms": 0,
        "input_tokens": 140,
        "output_tokens": 35,
        "estimated_cost_micro_usd": 70,
        "correct_judgments": 7,
    }
    assert report["batch"] == {
        "calls": 3,
        "elapsed_ms": 0,
        "input_tokens": 105,
        "output_tokens": 30,
        "estimated_cost_micro_usd": 57,
        "correct_judgments": 6,
    }
    judgments = report["judgments"]
    assert isinstance(judgments, list)
    assert {
        "id": "budget-wrong-citation",
        "expected_supported": False,
        "individual_supported": False,
        "batch_supported": True,
    } in judgments


def test_preflight_and_insufficient_budget_never_load_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden() -> None:
        raise AssertionError("an offline or denied run cannot load credentials")

    monkeypatch.setattr(benchmark, "load_evaluation_generation_key", forbidden)
    assert benchmark.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["individual_calls"] == 7 and plan["batch_calls"] == 3
    assert 0 < plan["reserved_cost_micro_usd"] <= 80_000

    assert (
        benchmark.main(["--execute", "--confirm-public-synthetic", "--max-spend-usd", "0.01"]) == 2
    )
    assert "reservation exceeds" in capsys.readouterr().err

    with pytest.raises(SystemExit, match="2"):
        benchmark.main(["--execute", "--max-spend-usd", "0.08"])
    assert "--confirm-public-synthetic" in capsys.readouterr().err
