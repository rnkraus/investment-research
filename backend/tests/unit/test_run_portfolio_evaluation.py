"""Exercise fresh evaluation execution without credentials or paid providers."""

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest

from investment_research.bootstrap import ApiContainer
from investment_research.modules.documents.contracts import (
    DocumentListEntry,
    DocumentListPage,
    DocumentProcessingSnapshot,
)
from investment_research.modules.documents.domain import OriginalMediaType, SourceLocatorKind
from investment_research.modules.identity.application import FixedLocalIdentityContextProvider
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    ProviderCostPolicy,
    ProviderOperationAttempt,
)
from investment_research.modules.operations.domain import ProviderOperationKind, ProviderUsage
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationSource,
    ResearchModel,
)
from scripts import run_portfolio_evaluation as runner
from scripts.evaluation_profiles import ResolvedEvaluationProfile, resolve_profile
from scripts.portfolio_live_runtime import (
    LiveEvaluationError,
    RunBudget,
    RunBudgetExceededError,
    composition_for,
    sources_for,
)
from tests.unit.test_evaluation_profiles import prepared as prepared
from tests.unit.test_evaluation_profiles import write_toml


@pytest.fixture
def live(prepared: tuple[Path, dict[str, Any]]) -> ResolvedEvaluationProfile:
    path, values = prepared
    values["mode"] = "retrieval_live"
    values["cases"]["include"] = ["A06"]
    values["budget"] = {"max_spend_usd": "0.08", "max_provider_calls": 8}
    write_toml(path, values)
    return resolve_profile(path)


def attempt() -> ProviderOperationAttempt:
    return ProviderOperationAttempt(
        correlation_id=UUID(int=99),
        research_request_id=None,
        processing_run_id=None,
        operation=ProviderOperationKind.QUERY_EMBEDDING,
        provider="openai",
        model="test",
        configuration_version="test-v1",
        maximum_usage=ProviderUsage(input_units=100, output_units=0),
        cost_policy=ProviderCostPolicy(
            input_micro_usd_per_million_units=1_000_000, output_micro_usd_per_million_units=0
        ),
    )


def test_run_budget_stops_before_canonical_reservation_or_network() -> None:
    budget = RunBudget(99, 10)
    reserve, finalize, tracer, call = Mock(), Mock(), Mock(), Mock()
    executor = ExecuteProviderOperationService(
        reserve, finalize, tracer, admit_attempt=budget.admit
    )
    with pytest.raises(RunBudgetExceededError):
        executor.execute(
            FixedLocalIdentityContextProvider().resolve(), attempt(), call, Mock(), Mock()
        )
    reserve.reserve.assert_not_called()
    tracer.start_span.assert_not_called()
    call.assert_not_called()
    assert budget.exhausted and budget.calls == 0


def test_dense_queries_and_failed_attempts_consume_one_shared_call_ceiling() -> None:
    budget = RunBudget(1_000, 2)
    budget.admit(attempt())
    provider = Mock()
    provider.retrieve.side_effect = RuntimeError("sensitive connection details")
    with pytest.raises(RuntimeError):
        budget.wrap_dense(provider).retrieve(Mock(), Mock())
    with pytest.raises(RunBudgetExceededError):
        budget.admit(attempt())
    assert budget.calls == 2 and budget.reserved_micro_usd == 100
    assert len(budget.attempts) == 2
    assert "sensitive" not in json.dumps(budget.attempts)


def test_every_supported_retrieval_switch_is_explicit(live: ResolvedEvaluationProfile) -> None:
    retrieval = live.profile.retrieval.model_copy(
        update={
            "lexical_strategy": "weighted_or",
            "text_neighbors": False,
            "page_expansion": False,
            "company_priority": False,
        }
    )
    changed = live.profile.model_copy(update={"retrieval": retrieval})
    options = composition_for(replace(live, profile=changed), RunBudget(500, 10))
    assert options.lexical_strategy == "weighted_or"
    assert (
        not options.text_neighbors and not options.page_expansion and not options.company_priority
    )
    assert options.admit_provider_attempt is not None and options.wrap_dense_retriever is not None


def stub_container(live: ResolvedEvaluationProfile) -> tuple[ApiContainer, AnswerGenerationInput]:
    now = datetime(2026, 11, 18, tzinfo=UTC)
    entries = tuple(
        DocumentListEntry(
            title=title,
            document_id=UUID(int=index),
            document_version_id=UUID(int=index + 100),
            media_type=OriginalMediaType.PDF,
            byte_size=10,
            created_at=now,
            captured_at=now,
            processing=DocumentProcessingSnapshot(
                processing_run_id=UUID(int=index + 200),
                document_version_id=UUID(int=index + 100),
                state=ProcessingState.SEARCHABLE,
                error_summary=None,
            ),
        )
        for index, title in enumerate(live.catalog.sources.values(), 1)
    )
    target = live.cases[0].context_targets[0]
    source = next(item for item in entries if item.title == live.catalog.sources[target.source])
    prepared = AnswerGenerationInput(
        research_request_id=UUID(int=901),
        question=live.cases[0].question,
        sources=(
            AnswerGenerationSource(
                retrieval_candidate_id=UUID(int=902),
                chunk_id=UUID(int=903),
                document_id=source.document_id,
                document_version_id=source.document_version_id,
                structure_block_id=UUID(int=904),
                source_locator_kind=SourceLocatorKind.PDF_PAGE,
                page_number=target.pages[0] or 1,
                text=" ".join(target.markers),
            ),
        ),
    )
    container = Mock(spec=ApiContainer)
    container.list_documents_service = Mock()
    container.retrieve_research_service = Mock()
    container.answer_research_request_service = Mock()
    container.identity_context_provider = FixedLocalIdentityContextProvider()
    container.list_documents_service.list_documents.return_value = DocumentListPage(items=entries)
    generation = Mock()
    generation.prepare_generation_input.return_value = prepared
    container.answer_research_request_service.model_services = {
        (ResearchModel.TERRA, ResearchModel.TERRA): generation,
        (ResearchModel.LUNA, ResearchModel.TERRA): generation,
    }
    return container, prepared


def test_fresh_retrieval_runs_app_search_but_never_generates_an_answer(
    live: ResolvedEvaluationProfile, tmp_path: Path
) -> None:
    container, _ = stub_container(live)
    result = runner.collect(container, live, RunBudget(80_000, 8), tmp_path)
    assert result["status"] == "completed" and result["observations"] == 1
    container.retrieve_research_service.retrieve.assert_called_once()  # type: ignore[attr-defined]
    container.answer_research_request_service.answer.assert_not_called()  # type: ignore[attr-defined]
    snapshot = json.loads((tmp_path / "snapshot.json").read_text())
    assert snapshot["selections"][0]["question"] == live.cases[0].question
    assert snapshot["selections"][0]["stage"] == "final_generation_context"
    assert snapshot["settings"]["text_neighbors"] is True
    assert json.loads((tmp_path / "results.jsonl").read_text())["manual_answer_review_required"]


def test_collector_persists_matching_case_journal_and_run_usage(
    live: ResolvedEvaluationProfile, tmp_path: Path
) -> None:
    from tests.unit.test_evaluation_usage import completion

    container, _ = stub_container(live)
    budget = RunBudget(80_000, 8)
    retrieved = container.retrieve_research_service.retrieve.return_value  # type: ignore[attr-defined]

    def retrieve(*args: object) -> object:
        operation = attempt()
        budget.admit(operation)
        budget.completed(operation, completion())
        return retrieved

    container.retrieve_research_service.retrieve.side_effect = retrieve  # type: ignore[attr-defined]
    result = runner.collect(container, live, budget, tmp_path)
    record = json.loads((tmp_path / "results.jsonl").read_text())
    assert result["provider_calls"] == 1
    assert result["reserved_model_cost_micro_usd"] == 100
    assert result["provider_usage"] == record["provider_usage"]
    assert result["usage_by_case"] == [
        {"case": "A06", "repetition": 1, "archive_mode": None, "usage": record["provider_usage"]}
    ]
    assert record["provider_usage"]["groups"][0]["input_tokens"] == 20
    assert record["provider_usage"]["groups"][0]["estimated_usage_cost_micro_usd"] == 20


def test_rag_execution_passes_selected_models_scope_and_optional_calls(
    live: ResolvedEvaluationProfile,
) -> None:
    profile = live.profile.model_copy(
        update={
            "mode": "rag_live",
            "scope": "catalog_documents",
            "models": live.profile.models.model_copy(update={"answer": "gpt-5.6-luna"}),
            "reranking": live.profile.reranking.model_copy(update={"strategy": "luna"}),
            "retrieval": live.profile.retrieval.model_copy(update={"decompose_query": True}),
        }
    )
    resolved = replace(live, profile=profile)
    container, prepared = stub_container(resolved)
    container.answer_research_request_service.answer.side_effect = (  # type: ignore[attr-defined]
        lambda *args, **kwargs: (
            kwargs["on_context"](prepared),
            SimpleNamespace(
                answer=SimpleNamespace(state=SimpleNamespace(value="STREAMED")),
                model_dump=lambda **kwargs: {"state": "STREAMED"},
            ),
        )[1]
    )
    entries = container.list_documents_service.list_documents.return_value.items  # type: ignore[attr-defined]
    row, _ = runner.collect_case(
        container, resolved, sources_for(resolved, entries), resolved.cases[0], 1
    )
    assert row is not None
    command = container.answer_research_request_service.answer.call_args.args[1]  # type: ignore[attr-defined]
    assert command.generation_model is ResearchModel.LUNA
    assert command.support_model is ResearchModel.TERRA
    assert command.rerank_with_luna and command.decompose_query
    assert command.document_ids


def test_changed_inventory_aborts_and_retains_failure_manifest(
    live: ResolvedEvaluationProfile, tmp_path: Path
) -> None:
    container, _ = stub_container(live)
    listing = container.list_documents_service.list_documents
    listing.side_effect = [listing.return_value, DocumentListPage(items=())]  # type: ignore[attr-defined]
    result = runner.collect(container, live, RunBudget(80_000, 8), tmp_path)
    assert result["status"] == "failed" and result["observations"] == 0
    container.retrieve_research_service.retrieve.assert_not_called()  # type: ignore[attr-defined]
    assert (tmp_path / "manifest.json").is_file()


def test_cli_preview_does_not_load_environment_or_start_app(
    live: ResolvedEvaluationProfile,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runner, "resolve_profile", lambda *args, **kwargs: live)
    load, build = Mock(), Mock()
    monkeypatch.setattr(runner, "load_api_settings", load)
    monkeypatch.setattr(runner, "build_api_container", build)
    monkeypatch.setattr(sys, "argv", ["runner"])
    assert runner.main() == 0
    load.assert_not_called()
    build.assert_not_called()
    assert json.loads(capsys.readouterr().out)["provider_calls_made"] == 0
    monkeypatch.setattr(sys, "argv", ["runner", "--execute"])
    assert runner.main() == 1
    load.assert_not_called()


def test_discovery_is_rejected_before_runtime_loading(live: ResolvedEvaluationProfile) -> None:
    case = next(item for item in live.catalog.cases if item.kind == "discovery")
    with pytest.raises(LiveEvaluationError, match=r"explicit archive\.case_filters"):
        runner.preview(replace(live, cases=(case,)))


def test_failure_redacts_exception_messages_and_retains_failed_observation(
    live: ResolvedEvaluationProfile, tmp_path: Path
) -> None:
    container, _ = stub_container(live)
    container.retrieve_research_service.retrieve.side_effect = RuntimeError(  # type: ignore[attr-defined]
        "credential-or-provider-content-must-not-escape"
    )
    result = runner.collect(container, live, RunBudget(80_000, 8), tmp_path)
    assert result["status"] == "completed" and result["failed_observations"] == 1
    journal = (tmp_path / "results.jsonl").read_text()
    assert "credential-or-provider-content" not in journal
    assert json.loads(journal)["exception_type"] == "RuntimeError"


def test_wrapped_budget_failure_still_stops_all_remaining_cases(
    live: ResolvedEvaluationProfile, tmp_path: Path
) -> None:
    container, _ = stub_container(live)
    budget = RunBudget(0, 8)
    container.retrieve_research_service.retrieve.side_effect = lambda *args: budget.admit(attempt())  # type: ignore[attr-defined]
    resolved = replace(live, profile=live.profile.model_copy(update={"repetitions": 2}))
    result = runner.collect(container, resolved, budget, tmp_path)
    assert result["status"] == "budget_exhausted"
    container.retrieve_research_service.retrieve.assert_called_once()  # type: ignore[attr-defined]
    assert (tmp_path / "manifest.json").is_file()
