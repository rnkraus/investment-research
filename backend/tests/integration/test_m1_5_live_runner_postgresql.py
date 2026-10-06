"""Exercise live-runner wiring with PostgreSQL and deterministic, zero-spend ports."""

from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import Engine

from investment_research.infrastructure.search.deterministic_index import (
    DeterministicSqliteDenseIndex,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationResult,
    ResearchModel,
    UnvalidatedEvidenceReference,
    UnvalidatedGeneratedAnswer,
    UnvalidatedGeneratedStatement,
)
from investment_research.modules.research.domain import CoverageStatus
from investment_research.modules.search.ports import SearchProviderTransientError
from scripts.m1_5_live_index import IsolatedEvaluationIndex
from scripts.m1_5_live_providers import (
    BudgetedAnswerGenerator,
    BudgetedEmbeddingProvider,
    BudgetedSupportEvaluator,
    LiveProviderCalls,
)
from scripts.m1_5_live_runner import run_live_baseline
from scripts.plan_m1_5_live_baseline import LiveBaselineBudget, build_plan
from scripts.verify_m1_5_corpus import DEFAULT_MANIFEST, REPOSITORY_ROOT, validate_corpus
from tests.integration.m1_5 import embedding_provider
from tests.integration.test_m1_5_answer_baseline_postgresql import ReviewedSupportEvaluator

MARKER = "synthetic-private-provider-error"


class ImmediateIndex(DeterministicSqliteDenseIndex):
    """Only the runner wiring is under test; live namespace cleanup has separate unit tests."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())

    def wait_until_visible(self) -> None:
        pass


class ScriptedGenerator:
    """Use reviewed answers, not a model-quality result or live-provider impersonation."""

    def __init__(self, *, fail_first: bool = False, altered_quote: bool = False) -> None:
        self.manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
        self.fail_first = fail_first
        self.altered_quote = altered_quote
        self.count = 0

    def generate(self, request: AnswerGenerationInput) -> AnswerGenerationResult:
        self.count += 1
        if self.fail_first and self.count == 1:
            raise RuntimeError(MARKER)
        case = next(case for case in self.manifest.cases if case.question == request.question)
        references = []
        for group in case.evidence_groups:
            passage = next(passage for passage in self.manifest.passages if passage.id == group[0])
            source = next(source for source in request.sources if passage.text in source.text)
            start = source.text.index(passage.text)
            references.append(
                UnvalidatedEvidenceReference(
                    retrieval_candidate_id=source.retrieval_candidate_id,
                    start_character=start,
                    end_character=start + len(passage.text),
                    exact_text=("X" + passage.text[1:] if self.altered_quote else passage.text),
                )
            )
        insufficient = case.expected_coverage == "INSUFFICIENT"
        proposal = UnvalidatedGeneratedAnswer(
            coverage_status=CoverageStatus(case.expected_coverage),
            rendered_text="Scripted reviewed proposal.",
            statements=()
            if insufficient
            else (
                UnvalidatedGeneratedStatement(
                    text=case.expected_answer or "",
                    evidence=tuple(references),
                ),
            ),
            missing_information="No reviewed evidence." if insufficient else None,
        )
        return AnswerGenerationResult(
            answer=proposal,
            usage=ProviderUsage(input_units=10, output_units=5),
        )


@pytest.mark.parametrize("failure", [None, "budget", "embedding", "answer", "quote"])
def test_runner_records_every_case_and_never_publishes_unvalidated_output(
    integration_database_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    plan = build_plan(
        answer_model=ResearchModel.LUNA,
        support_model=ResearchModel.LUNA,
        maximum_spend_micro_usd=1 if failure == "budget" else 300_000,
    )
    calls = LiveProviderCalls(LiveBaselineBudget(plan))
    embedding = embedding_provider()
    if failure == "embedding":

        def fail(*args: object) -> None:
            raise SearchProviderTransientError(MARKER)

        monkeypatch.setattr(type(embedding), "embed", fail)
    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    index = ImmediateIndex(tmp_path / "index.sqlite")
    result = run_live_baseline(
        integration_database_engine,
        plan,
        index=cast(IsolatedEvaluationIndex, index),
        document_embedding=BudgetedEmbeddingProvider(embedding, calls),
        query_embedding=BudgetedEmbeddingProvider(embedding, calls),
        generator=BudgetedAnswerGenerator(
            ScriptedGenerator(fail_first=failure == "answer", altered_quote=failure == "quote"),
            calls,
        ),
        support=BudgetedSupportEvaluator(ReviewedSupportEvaluator(manifest), calls),
        calls=calls,
    )
    assert result.summary.case_count == len(result.observations.cases) == 6
    assert result.reserved_cost_micro_usd <= plan.maximum_spend_micro_usd
    assert result.summary.support_assessed_count == 0
    assert result.cleanup_status == "PENDING"
    assert MARKER not in result.model_dump_json()
    if failure is None:
        assert not result.failures
        assert result.summary.coverage_match_count == 6
        assert result.summary.citation_match_count == 6
        assert result.summary.abstention_match_count == result.summary.conflict_match_count == 1
        assert all(call.usage_state == "REPORTED" for call in result.provider_calls)
    elif failure in {"budget", "embedding"}:
        assert result.preparation_failed
        assert len(result.failures) == 6
        assert all(
            item.reason == ("budget" if failure == "budget" else "provider")
            for item in result.failures
        )
        assert all(case.terminal_status == "FAILED" for case in result.observations.cases)
        if failure == "embedding":
            assert result.total_usage.estimated_cost_micro_usd is None
            assert result.preparation_usage.input_tokens is None
    elif failure == "answer":
        assert len(result.failures) == 1
        assert result.failures[0].reason == "provider"
        assert result.observations.cases[0].provider_usage is not None
        assert result.observations.cases[0].provider_usage.estimated_cost_micro_usd is None
        assert result.total_usage.estimated_cost_micro_usd is None
        assert result.summary.coverage_match_count == 5
    else:
        assert len(result.failures) == 5
        assert all(item.reason == "rejected" for item in result.failures)
        assert result.summary.exact_citation_checked_count == 0
        assert result.summary.abstention_match_count == 1
