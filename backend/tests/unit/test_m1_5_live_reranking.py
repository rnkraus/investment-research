"""Reranking evaluation admission and accounting stay offline and credential-free."""

import json
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from investment_research.modules.operations.domain import ProviderOperationKind, ProviderUsage
from investment_research.modules.search.contracts import (
    RerankCandidate,
    RerankRequest,
    RerankResponse,
    RerankResult,
)
from investment_research.modules.search.ports import SearchProviderTransientError
from investment_research.modules.search.reranking_contracts import (
    BGE_RERANK_CONFIG_VERSION,
    BGE_RERANK_MODEL,
    RrfRerankingProfile,
)
from investment_research.settings import RerankingProviderSettings
from scripts import m1_5_live_runtime as runtime
from scripts import verify_m1_5_live_baseline as cli
from scripts.m1_5_live_providers import BudgetedReranker, LiveProviderCallError, LiveProviderCalls
from scripts.m1_5_live_runner import summarize_usage
from scripts.plan_m1_5_live_baseline import (
    LiveBaselineBudget,
    LiveBaselineLimitError,
    build_plan,
    retrieval_policy_for_version,
    selection_profile_for_policy,
)
from tests.unit.test_verify_m1_5_live_baseline import report, settings


@pytest.fixture(autouse=True)
def reject_external_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline test attempted external access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    for name in (
        "load_evaluation_settings",
        "load_evaluation_reranking_settings",
        "provider_resources",
        "isolated_database",
    ):
        monkeypatch.setattr(cli, name, forbidden)


def request() -> RerankRequest:
    return RerankRequest(
        normalized_query="Synthetic question",
        candidates=(RerankCandidate(retrieval_candidate_id=uuid4(), text="Synthetic source"),),
        reranker_config_version=BGE_RERANK_CONFIG_VERSION,
    )


@dataclass
class RecordingReranker:
    calls: int = 0
    fail: bool = False
    units: int = 1

    def rerank(self, value: RerankRequest) -> RerankResponse:
        self.calls += 1
        if self.fail:
            raise SearchProviderTransientError("synthetic provider failure")
        return RerankResponse(
            results=tuple(
                RerankResult(
                    retrieval_candidate_id=item.retrieval_candidate_id,
                    rank=ordinal,
                    score=1.0 / ordinal,
                )
                for ordinal, item in enumerate(value.candidates, 1)
            ),
            usage=ProviderUsage(input_units=self.units, output_units=0),
            model=BGE_RERANK_MODEL,
            reranker_config_version=BGE_RERANK_CONFIG_VERSION,
        )


def admitted_settings() -> RerankingProviderSettings:
    return RerankingProviderSettings(
        api_key=SecretStr("synthetic-rerank-key"),
        monthly_request_limit=6,
        starter_allowance_confirmed=True,
        public_synthetic_content_approved=True,
    )


def test_reranking_plan_preserves_generation_bounds_and_adds_separate_limits() -> None:
    original = build_plan(retrieval_policy="rrf", answer_policy="citable_units")
    selected = build_plan(retrieval_policy="rrf_rerank", answer_policy="citable_units")
    assert selected.reranking_limit is not None
    assert selected.reranking_limit.maximum_requests == 6
    assert selected.reranking_limit.maximum_rerank_units_per_request == 1
    assert selected.model_dump(exclude={"configuration", "reranking_limit"}) == (
        original.model_dump(exclude={"configuration", "reranking_limit"})
    )
    assert selected.configuration.model_dump(exclude={"selector_config_version"}) == (
        original.configuration.model_dump(exclude={"selector_config_version"})
    )
    assert isinstance(selection_profile_for_policy("rrf_rerank"), RrfRerankingProfile)
    assert (
        retrieval_policy_for_version(selected.configuration.selector_config_version) == "rrf_rerank"
    )


@pytest.mark.parametrize("fail", [False, True])
def test_six_attempt_limit_counts_failed_calls_without_retries(fail: bool) -> None:
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan(retrieval_policy="rrf_rerank")))
    delegate = RecordingReranker(fail=fail)
    provider = BudgetedReranker(delegate, calls)
    for _ in range(6):
        if fail:
            with pytest.raises(SearchProviderTransientError):
                provider.rerank(request())
        else:
            provider.rerank(request())
    with pytest.raises(LiveBaselineLimitError, match="reranking limit"):
        provider.rerank(request())
    assert delegate.calls == len(calls.records) == 6
    assert all(item.role is ProviderOperationKind.RERANKING for item in calls.records)
    assert calls.budget.reserved_cost_micro_usd == 0
    assert all(item.input_tokens is None and item.output_tokens is None for item in calls.records)
    assert all(item.elapsed_ms is not None for item in calls.records)
    totals = summarize_usage(calls.records)
    assert totals.input_tokens == totals.output_tokens == 0
    assert totals.rerank_units == (None if fail else 6)
    assert totals.estimated_cost_micro_usd == (None if fail else 0)
    assert all(
        item.reported_model == (None if fail else BGE_RERANK_MODEL) for item in calls.records
    )


@pytest.mark.parametrize("units", [0, 2])
def test_unexpected_usage_halts_the_run_without_a_second_call(units: int) -> None:
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan(retrieval_policy="rrf_rerank")))
    delegate = RecordingReranker(units=units)
    provider = BudgetedReranker(delegate, calls)
    with pytest.raises(LiveProviderCallError):
        provider.rerank(request())
    with pytest.raises(LiveBaselineLimitError):
        provider.rerank(request())
    assert delegate.calls == 1
    assert calls.records[0].usage_state == "USAGE_REJECTED"


@pytest.mark.parametrize("policy", ["m1", "rrf"])
def test_original_policies_cannot_call_reranking(policy: str) -> None:
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan(retrieval_policy=policy)))  # type: ignore[arg-type]
    delegate = RecordingReranker()
    with pytest.raises(LiveBaselineLimitError):
        BudgetedReranker(delegate, calls).rerank(request())
    assert delegate.calls == 0
    assert calls.records == ()


def test_reranking_preflight_has_no_runtime_or_credential_access(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--retrieval-policy", "rrf_rerank"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["reranking_limit"]["model"] == BGE_RERANK_MODEL
    assert plan["maximum_spend_micro_usd"] == 800_000
    assert "api_key" not in json.dumps(plan)


def test_missing_allowance_confirmation_fails_before_output_or_configuration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "not-created.json"
    assert (
        cli.main(
            [
                "--execute",
                "--retrieval-policy",
                "rrf_rerank",
                "--confirm-public-synthetic",
                "--confirm-pinecone-starter",
                "--output",
                str(path),
            ]
        )
        == 2
    )
    assert not path.exists()


def test_cli_passes_separate_admitted_settings_and_reranker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "load_evaluation_settings", settings)
    config = admitted_settings()
    loader = MagicMock(return_value=config)
    monkeypatch.setattr(cli, "load_evaluation_reranking_settings", loader)
    port = RecordingReranker()

    @contextmanager
    def database() -> Iterator[MagicMock]:
        yield MagicMock()

    @contextmanager
    def resources(*args: object, **kwargs: object) -> Iterator[MagicMock]:
        assert kwargs["reranking_settings"] is config
        yield MagicMock(reranker=port)

    monkeypatch.setattr(cli, "isolated_database", database)
    monkeypatch.setattr(cli, "provider_resources", resources)
    runner = MagicMock(return_value=report())
    monkeypatch.setattr(cli, "run_live_baseline", runner)
    assert (
        cli.main(
            [
                "--execute",
                "--retrieval-policy",
                "rrf_rerank",
                "--confirm-public-synthetic",
                "--confirm-pinecone-starter",
                "--confirm-reranking-allowance",
                "--output",
                str(tmp_path / "new.json"),
            ]
        )
        == 0
    )
    loader.assert_called_once_with(
        monthly_request_limit=6,
        starter_allowance_confirmed=True,
        public_synthetic_content_approved=True,
    )
    assert runner.call_args.kwargs["reranker"] is port


@pytest.mark.parametrize(
    "config", [None, RerankingProviderSettings(api_key=SecretStr("synthetic"))]
)
def test_runtime_rejects_missing_or_disabled_admission_before_provider_setup(
    monkeypatch: pytest.MonkeyPatch,
    config: RerankingProviderSettings | None,
) -> None:
    factory = MagicMock(side_effect=AssertionError("provider setup must not run"))
    monkeypatch.setattr(runtime, "create_openai_embedding_provider", factory)
    worker, api = settings()
    plan = build_plan(retrieval_policy="rrf_rerank")
    with (
        pytest.raises(runtime.LiveRuntimeError),
        runtime.provider_resources(
            worker,
            api,
            plan,
            LiveProviderCalls(LiveBaselineBudget(plan)),
            [],
            reranking_settings=config,
        ),
    ):
        pytest.fail("unapproved reranking must not yield resources")
    factory.assert_not_called()


def test_runtime_constructs_reranker_with_only_its_dedicated_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, api = settings()
    config = admitted_settings()
    plan = build_plan(retrieval_policy="rrf_rerank")
    factory = MagicMock(return_value=RecordingReranker())
    monkeypatch.setattr(runtime, "create_pinecone_reranker", factory)
    for name in (
        "create_openai_embedding_provider",
        "create_openai_answer_generator",
        "create_openai_statement_support_evaluator",
        "create_pinecone_dense_index_writer",
        "create_pinecone_dense_retriever",
        "IsolatedEvaluationIndex",
    ):
        monkeypatch.setattr(runtime, name, MagicMock(return_value=MagicMock()))
    with runtime.provider_resources(
        worker,
        api,
        plan,
        LiveProviderCalls(LiveBaselineBudget(plan)),
        [],
        reranking_settings=config,
    ) as value:
        assert value.reranker is not None
    factory.assert_called_once_with(api_key=config.api_key, operation_timeout_seconds=20)
