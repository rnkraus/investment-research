"""Credential-free regression tests for live evaluation admission and accounting."""

import argparse
import json
import socket
from dataclasses import replace

import dotenv
import pytest

from investment_research.infrastructure.evidence.openai import (
    LOCAL_STATEMENT_SUPPORT_REASON_PROMPT_VERSION,
    M1_5_ATTRIBUTION_SUPPORT_PROMPT_VERSION,
    M1_5_DOCUMENT_ATTRIBUTION_SUPPORT_PROMPT_VERSION,
    M1_5_LOCAL_ATTRIBUTION_SUPPORT_REASON_PROMPT_VERSION,
    M1_5_LOCAL_DOCUMENT_ATTRIBUTION_SUPPORT_REASON_PROMPT_VERSION,
    M1_STATEMENT_SUPPORT_PROMPT_VERSION,
)
from investment_research.modules.operations.domain import ProviderOperationKind, ProviderUsage
from investment_research.modules.research.contracts import ResearchModel
from investment_research.modules.search.fusion_contracts import RrfSelectionProfile
from scripts import plan_m1_5_live_baseline as live
from scripts.verify_m1_5_corpus import DEFAULT_MANIFEST, REPOSITORY_ROOT, validate_corpus


def test_plan_freezes_the_six_reviewed_cases_and_existing_m1_policy() -> None:
    plan = live.build_plan()

    assert plan.execution_mode == "preflight_only"
    assert plan.corpus_sha256 == live.APPROVED_CORPUS_SHA256
    assert plan.document_count == 3
    assert len(plan.case_ids) == len(set(plan.case_ids)) == 6
    assert plan.configuration.selector_config_version == "m1-lexical-first-round-robin-v1"
    assert plan.configuration.answer_prompt_version == "m1-grounded-answer-prompt-v4"
    assert plan.maximum_spend_micro_usd == 800_000
    assert {limit.role: limit.maximum_requests for limit in plan.role_limits} == {
        ProviderOperationKind.DOCUMENT_EMBEDDING: 64,
        ProviderOperationKind.QUERY_EMBEDDING: 6,
        ProviderOperationKind.ANSWER_GENERATION: 6,
        ProviderOperationKind.STATEMENT_SUPPORT: 48,
    }


def test_rrf_plan_changes_only_the_selector_configuration() -> None:
    m1 = live.build_plan()
    rrf = live.build_plan(retrieval_policy="rrf")

    assert rrf.configuration.selector_config_version == "m1-5-rrf-k60-v1"
    assert isinstance(live.selection_profile_for_policy("rrf"), RrfSelectionProfile)
    assert live.retrieval_policy_for_version(rrf.configuration.selector_config_version) == "rrf"
    assert live.retrieval_policy_for_version(m1.configuration.selector_config_version) == "m1"
    assert rrf.model_dump(exclude={"configuration"}) == m1.model_dump(exclude={"configuration"})
    assert rrf.configuration.model_dump(exclude={"selector_config_version"}) == (
        m1.configuration.model_dump(exclude={"selector_config_version"})
    )


@pytest.mark.parametrize("explanations", [False, True])
@pytest.mark.parametrize("support_policy", ["attribution", "document_attribution"])
def test_attribution_policy_is_explicit_versioned_and_does_not_raise_limits(
    explanations: bool,
    support_policy: live.LiveSupportPolicy,
) -> None:
    original = live.build_plan(support_explanations=explanations)
    selected = live.build_plan(support_policy=support_policy, support_explanations=explanations)
    expected = (
        M1_5_LOCAL_ATTRIBUTION_SUPPORT_REASON_PROMPT_VERSION
        if explanations
        else M1_5_ATTRIBUTION_SUPPORT_PROMPT_VERSION
    )
    if support_policy == "document_attribution":
        expected = (
            M1_5_LOCAL_DOCUMENT_ATTRIBUTION_SUPPORT_REASON_PROMPT_VERSION
            if explanations
            else M1_5_DOCUMENT_ATTRIBUTION_SUPPORT_PROMPT_VERSION
        )
    assert selected.configuration.support_prompt_version == expected
    assert (
        live.support_policy_for_prompt_version(selected.configuration.support_prompt_version)
        == support_policy
    )
    assert (
        live.support_policy_for_prompt_version(original.configuration.support_prompt_version)
        == "m1"
    )
    assert selected.model_dump(exclude={"configuration"}) == original.model_dump(
        exclude={"configuration"}
    )
    assert selected.configuration.model_dump(
        exclude={"support_prompt_version"}
    ) == original.configuration.model_dump(exclude={"support_prompt_version"})


def test_unknown_support_policy_or_prompt_is_rejected() -> None:
    with pytest.raises(live.LiveBaselineLimitError):
        live.build_plan(support_policy="unknown")  # type: ignore[arg-type]
    with pytest.raises(live.LiveBaselineLimitError):
        live.support_policy_for_prompt_version("unknown")


def test_citable_policy_changes_only_the_frozen_answer_prompt() -> None:
    original = live.build_plan(retrieval_policy="rrf")
    selected = live.build_plan(retrieval_policy="rrf", answer_policy="citable_units")
    assert selected.configuration.answer_prompt_version == "m1-5-citable-units-en-answer-prompt-v4"
    assert selected.model_dump(exclude={"configuration"}) == original.model_dump(
        exclude={"configuration"}
    )
    assert selected.configuration.model_dump(
        exclude={"answer_prompt_version"}
    ) == original.configuration.model_dump(exclude={"answer_prompt_version"})
    assert (
        live.answer_policy_for_prompt_version(selected.configuration.answer_prompt_version)
        == "citable_units"
    )
    assert (
        live.answer_policy_for_prompt_version(original.configuration.answer_prompt_version) == "m1"
    )
    assert (
        live.answer_profile_for_policy(
            "citable_units", ResearchModel.LUNA
        ).generation_config_version
        == "m1-5-gpt-5.6-luna-low-citable-units-en-v4"
    )
    with pytest.raises(live.LiveBaselineLimitError):
        live.answer_policy_for_prompt_version("unknown")
    with pytest.raises(live.LiveBaselineLimitError):
        live.build_plan(answer_policy="unknown")  # type: ignore[arg-type]


def test_unknown_policy_and_selector_version_are_rejected() -> None:
    with pytest.raises(live.LiveBaselineLimitError, match="policy"):
        live.build_plan(retrieval_policy="unknown")  # type: ignore[arg-type]
    with pytest.raises(live.LiveBaselineLimitError, match="version"):
        live.retrieval_policy_for_version("unknown")


def test_local_support_explanations_change_only_the_recorded_prompt_version() -> None:
    original = live.build_plan()
    explained = live.build_plan(support_explanations=True)
    assert original.configuration.support_prompt_version == M1_STATEMENT_SUPPORT_PROMPT_VERSION
    assert (
        explained.configuration.support_prompt_version
        == LOCAL_STATEMENT_SUPPORT_REASON_PROMPT_VERSION
    )
    assert explained.model_dump(exclude={"configuration"}) == original.model_dump(
        exclude={"configuration"}
    )
    assert explained.configuration.model_dump(
        exclude={"support_prompt_version"}
    ) == original.configuration.model_dump(exclude={"support_prompt_version"})


def test_preflight_cli_uses_no_network_or_environment_loader(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("preflight must not access providers or environment files")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(dotenv, "dotenv_values", forbidden)
    monkeypatch.setattr(
        "sys.argv", ["plan", "--max-spend-usd", "0.20", "--retrieval-policy", "rrf"]
    )

    assert live.main() == 0
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["maximum_spend_micro_usd"] == 200_000
    assert data["configuration"]["selector_config_version"] == "m1-5-rrf-k60-v1"
    assert "question" not in data
    assert "exact_text" not in output
    assert "expected_answer" not in output
    assert "api_key" not in output


def test_changed_catalog_digest_fails_before_corpus_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live, "APPROVED_CORPUS_SHA256", "0" * 64)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("unapproved catalog must not be parsed")

    monkeypatch.setattr(live, "validate_corpus", forbidden)
    with pytest.raises(live.LiveBaselineLimitError, match="not approved"):
        live.build_plan()


def test_owner_review_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    unreviewed = manifest.model_copy(update={"review_status": "pending_owner_review"})
    monkeypatch.setattr(live, "validate_corpus", lambda *args: unreviewed)

    with pytest.raises(live.LiveBaselineLimitError, match="owner review"):
        live.build_plan()


@pytest.mark.parametrize("amount", [0, -1, 800_001, 1_000_000, True, 0.5])
def test_plan_rejects_invalid_or_expanded_budget(amount: int) -> None:
    with pytest.raises(live.LiveBaselineLimitError, match="budget"):
        live.build_plan(maximum_spend_micro_usd=amount)


@pytest.mark.parametrize(
    "value",
    [
        "NaN",
        "Infinity",
        "-1",
        "0",
        "1.01",
        "0.800001",
        "0.81",
        "0.0000001",
        "no",
        "1e999999999",
        "1e-999999999",
        "0.10000000000000000000000000000001",
    ],
)
def test_cli_rejects_invalid_budget_without_rounding(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        live._budget_argument(value)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0.80", 800_000),
        ("0.50", 500_000),
        ("0.300001", 300_001),
        ("0.30", 300_000),
        ("0.10", 100_000),
        ("0.000001", 1),
    ],
)
def test_cli_parses_budget_in_exact_micro_dollars(value: str, expected: int) -> None:
    assert live._budget_argument(value) == expected


def test_model_pair_has_separate_prices_and_full_output_limits() -> None:
    plan = live.build_plan(answer_model=ResearchModel.LUNA, support_model=ResearchModel.TERRA)
    budget = live.LiveBaselineBudget(plan)

    answer = budget.reserve(ProviderOperationKind.ANSWER_GENERATION, payload_utf8_bytes=1)
    support = budget.reserve(ProviderOperationKind.STATEMENT_SUPPORT, payload_utf8_bytes=1)

    assert answer.input_units == support.input_units == 16_001
    assert answer.output_units == 2_000
    assert support.output_units == 1_000
    assert answer.estimated_cost_micro_usd == 5_601
    assert support.estimated_cost_micro_usd == 44_002
    assert budget.reserved_cost_micro_usd == 49_603


def test_embedding_price_rounds_up_and_reserves_no_output() -> None:
    budget = live.LiveBaselineBudget(live.build_plan())
    reservation = budget.reserve(ProviderOperationKind.QUERY_EMBEDDING, payload_utf8_bytes=1)

    assert reservation.input_units == 1
    assert reservation.output_units == 0
    assert reservation.estimated_cost_micro_usd == 1


def test_budget_boundary_is_inclusive_and_denied_call_changes_nothing() -> None:
    budget = live.LiveBaselineBudget(live.build_plan(maximum_spend_micro_usd=1))
    reservation = budget.reserve(ProviderOperationKind.QUERY_EMBEDDING, payload_utf8_bytes=1)
    budget.verify_usage(reservation, ProviderUsage(input_units=1, output_units=0))

    with pytest.raises(live.LiveBaselineLimitError, match="budget is exhausted"):
        budget.reserve(ProviderOperationKind.QUERY_EMBEDDING, payload_utf8_bytes=1)
    assert budget.reserved_cost_micro_usd == 1
    assert len(budget._reservations) == 1


def test_failure_without_usage_keeps_reservation_and_attempt_limit() -> None:
    budget = live.LiveBaselineBudget(live.build_plan())
    for _ in range(6):
        budget.reserve(ProviderOperationKind.QUERY_EMBEDDING, payload_utf8_bytes=100)

    with pytest.raises(live.LiveBaselineLimitError, match="request limit"):
        budget.reserve(ProviderOperationKind.QUERY_EMBEDDING, payload_utf8_bytes=100)
    assert budget.reserved_cost_micro_usd == 12


@pytest.mark.parametrize("size", [0, -1, True, 128_001])
def test_invalid_or_oversized_request_is_denied_before_reservation(size: int) -> None:
    budget = live.LiveBaselineBudget(live.build_plan())
    with pytest.raises(live.LiveBaselineLimitError):
        budget.reserve(ProviderOperationKind.QUERY_EMBEDDING, payload_utf8_bytes=size)
    assert budget.reserved_cost_micro_usd == 0


@pytest.mark.parametrize(
    "usage",
    [
        ProviderUsage(input_units=16_002, output_units=0),
        ProviderUsage(input_units=1, output_units=2_001),
    ],
)
def test_usage_overrun_halts_subsequent_calls(usage: ProviderUsage) -> None:
    budget = live.LiveBaselineBudget(live.build_plan())
    reservation = budget.reserve(ProviderOperationKind.ANSWER_GENERATION, payload_utf8_bytes=1)

    with pytest.raises(live.LiveBaselineLimitError, match="usage"):
        budget.verify_usage(reservation, usage)
    with pytest.raises(live.LiveBaselineLimitError, match="stopped"):
        budget.reserve(ProviderOperationKind.QUERY_EMBEDDING, payload_utf8_bytes=1)
    assert budget.reserved_cost_micro_usd == reservation.estimated_cost_micro_usd


def test_reservation_from_another_invocation_is_rejected() -> None:
    budget = live.LiveBaselineBudget(live.build_plan())
    other = replace(budget)
    reservation = other.reserve(ProviderOperationKind.QUERY_EMBEDDING, payload_utf8_bytes=1)

    with pytest.raises(live.LiveBaselineLimitError, match="usage"):
        budget.verify_usage(reservation, ProviderUsage(input_units=1, output_units=0))


def test_cli_failure_is_safe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["plan"])
    monkeypatch.setattr(live, "APPROVED_CORPUS_SHA256", "0" * 64)

    assert live.main() == 1
    assert capsys.readouterr().err == (
        "M1.5 live preflight failed; corpus and provider details were withheld.\n"
    )
