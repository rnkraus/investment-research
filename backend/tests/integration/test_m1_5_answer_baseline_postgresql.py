"""Observe the complete M1 answer application path with reviewed scripted providers."""

import hashlib
import os
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter_ns
from typing import Literal

import pytest
from sqlalchemy import Engine

from investment_research.infrastructure.database.session import create_session_factory
from investment_research.infrastructure.database.unit_of_work import (
    create_answer_validation_unit_of_work_factory,
    create_provider_operation_unit_of_work_factory,
    create_research_retrieval_unit_of_work_factory,
)
from investment_research.infrastructure.operations.noop import NoopOperationTracer
from investment_research.infrastructure.search.deterministic import (
    DeterministicRuleEmbeddingProvider,
)
from investment_research.infrastructure.search.deterministic_index import (
    DeterministicSqliteDenseIndex,
)
from investment_research.modules.evidence.contracts import (
    EvidenceValidationProfile,
    StatementSupportDecision,
    StatementSupportResult,
)
from investment_research.modules.evidence.persistence import SqlAlchemyEvidenceRepository
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    FinalizeProviderOperationService,
    ProviderBudgetPolicy,
    ProviderCostPolicy,
    ReserveProviderOperationService,
)
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.processing.application import M1_PROCESSING_PROFILE
from investment_research.modules.research.application import (
    AnswerResearchRequestService,
    GenerateValidatedAnswerService,
    M1QueryRetrievalDependencies,
    QueryEmbeddingAccountingProfile,
    RetrieveResearchService,
)
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationProfile,
    AnswerGenerationResult,
    ResearchRetrievalInput,
    UnvalidatedEvidenceReference,
    UnvalidatedGeneratedAnswer,
    UnvalidatedGeneratedStatement,
)
from investment_research.modules.research.domain import AnswerState, CoverageStatus
from investment_research.modules.research.persistence import SqlAlchemyResearchRepository
from investment_research.modules.search.contracts import (
    M1_LANE_CANDIDATE_LIMIT,
    DenseRetrievalProfile,
    LexicalSearchProfile,
    M1SelectionProfile,
    SelectedRetrievalCandidate,
)
from investment_research.modules.search.persistence import SqlAlchemyRetrievalRepository
from scripts.map_m1_5_passages import map_cited_passages, map_selected_passages
from scripts.score_m1_5_observations import (
    CaseObservation,
    ObservationSet,
    RunConfiguration,
    score_observations,
)
from scripts.verify_m1_5_corpus import (
    DEFAULT_MANIFEST,
    REPOSITORY_ROOT,
    EvaluationManifest,
    validate_corpus,
)
from tests.integration.factories import ResearchSource, make_research_source
from tests.integration.m1_5 import (
    embedding_provider,
    index_sources,
    persist_sources,
    source_for_catalog_document,
)

OUTPUT_PATH_ENVIRONMENT_NAME = "INVESTMENT_RESEARCH_M1_5_ANSWER_OUTPUT"
SCRIPTED_PROMPT_VERSION = "m1.5-reviewed-answer-v1"
SCRIPTED_SUPPORT_VERSION = "m1.5-reviewed-support-v1"
INSUFFICIENT_MESSAGE = "The archive does not contain sufficient evidence."
OBSERVED_COVERAGE: dict[
    CoverageStatus, Literal["COMPLETE", "PARTIAL", "CONFLICTING", "INSUFFICIENT"]
] = {
    CoverageStatus.COMPLETE: "COMPLETE",
    CoverageStatus.PARTIAL: "PARTIAL",
    CoverageStatus.CONFLICTING: "CONFLICTING",
    CoverageStatus.INSUFFICIENT: "INSUFFICIENT",
}


class ReviewedAnswerGenerator:
    """Script reviewed answers while resolving quotes only inside actual selected context."""

    def __init__(
        self,
        manifest: EvaluationManifest,
        sources: tuple[ResearchSource, ...],
        *,
        alter_quote: bool = False,
    ) -> None:
        self.manifest = manifest
        self.document_ids = dict(
            zip(
                (doc.id for doc in manifest.documents),
                (src.document.id for src in sources),
                strict=True,
            )
        )
        self.blocks = {block.id: block for source in sources for block in source.blocks}
        self.alter_quote = alter_quote
        self.inputs: list[AnswerGenerationInput] = []

    def generate(self, generation_input: AnswerGenerationInput) -> AnswerGenerationResult:
        self.inputs.append(generation_input)
        case = next(
            case for case in self.manifest.cases if case.question == generation_input.question
        )
        if case.expected_coverage == "INSUFFICIENT":
            proposal = UnvalidatedGeneratedAnswer(
                coverage_status=CoverageStatus.INSUFFICIENT,
                rendered_text=INSUFFICIENT_MESSAGE,
                statements=(),
                missing_information=INSUFFICIENT_MESSAGE,
            )
        else:
            assert case.expected_answer is not None
            references = tuple(
                self._reference(group, generation_input) for group in case.evidence_groups
            )
            if self.alter_quote:
                reference = references[0]
                references = (
                    reference.model_copy(update={"exact_text": "X" + reference.exact_text[1:]}),
                    *references[1:],
                )
            proposal = UnvalidatedGeneratedAnswer(
                coverage_status=CoverageStatus(case.expected_coverage),
                rendered_text="Untrusted provider rendering.",
                statements=(
                    UnvalidatedGeneratedStatement(text=case.expected_answer, evidence=references),
                ),
            )
        return AnswerGenerationResult(
            answer=proposal,
            usage=ProviderUsage(input_units=0, output_units=0),
        )

    def _reference(
        self,
        group: tuple[str, ...],
        generation_input: AnswerGenerationInput,
    ) -> UnvalidatedEvidenceReference:
        for passage in self.manifest.passages:
            if passage.id not in group:
                continue
            for source in generation_input.sources:
                block = self.blocks[source.structure_block_id]
                start = passage.source_anchor.start_character - block.start_character
                end = passage.source_anchor.end_character - block.start_character
                if (
                    source.document_id == self.document_ids[passage.document_id]
                    and source.source_locator_kind.value == passage.source_anchor.kind
                    and source.page_number == passage.source_anchor.page_number
                    and 0 <= start < end <= len(source.text)
                    and source.text[start:end] == passage.text
                ):
                    return UnvalidatedEvidenceReference(
                        retrieval_candidate_id=source.retrieval_candidate_id,
                        start_character=start,
                        end_character=end,
                        exact_text=passage.text,
                    )
        raise AssertionError("reviewed evidence was not present in selected context")


class ReviewedSupportEvaluator:
    """Exercise the support boundary using explicit fixture rules, without quality claims."""

    def __init__(self, manifest: EvaluationManifest, *, reject: bool = False) -> None:
        passages = {passage.id: passage.text for passage in manifest.passages}
        self.rules = {
            case.expected_answer: tuple(passages[group[0]] for group in case.evidence_groups)
            for case in manifest.cases
            if case.expected_answer is not None
        }
        self.reject = reject
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def evaluate(self, statement: str, evidence_texts: Sequence[str]) -> StatementSupportResult:
        exact_texts = tuple(evidence_texts)
        self.calls.append((statement, exact_texts))
        return StatementSupportResult(
            decision=StatementSupportDecision(
                supported=not self.reject and self.rules.get(statement) == exact_texts
            ),
            usage=ProviderUsage(input_units=0, output_units=0),
        )


def _make_service(
    engine: Engine,
    generator: ReviewedAnswerGenerator,
    support: ReviewedSupportEvaluator,
    embedding: DeterministicRuleEmbeddingProvider,
    index: DeterministicSqliteDenseIndex,
) -> AnswerResearchRequestService:
    session_factory = create_session_factory(engine)
    operation_factory = create_provider_operation_unit_of_work_factory(session_factory)
    executor = ExecuteProviderOperationService(
        reservation_service=ReserveProviderOperationService(
            unit_of_work_factory=operation_factory,
            budget_policy=ProviderBudgetPolicy(monthly_limit_micro_usd=5_000_000),
        ),
        finalization_service=FinalizeProviderOperationService(
            unit_of_work_factory=operation_factory
        ),
        tracer=NoopOperationTracer(),
    )
    retrieval = RetrieveResearchService(
        unit_of_work_factory=create_research_retrieval_unit_of_work_factory(session_factory),
        search_profile=LexicalSearchProfile(
            lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
            candidate_limit=M1_LANE_CANDIDATE_LIMIT,
        ),
        m1_query_retrieval=M1QueryRetrievalDependencies(
            embedding_provider=embedding,
            dense_retriever=index,
            dense_profile=DenseRetrievalProfile(
                embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
                dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
                candidate_limit=M1_LANE_CANDIDATE_LIMIT,
            ),
            selection_profile=M1SelectionProfile(),
            provider_operation_executor=executor,
            embedding_accounting=QueryEmbeddingAccountingProfile(
                provider="deterministic",
                model="reviewed-embedding-rules-v1",
                cost_policy=ProviderCostPolicy(
                    input_micro_usd_per_million_units=0,
                    output_micro_usd_per_million_units=0,
                ),
            ),
        ),
    )
    generation = GenerateValidatedAnswerService(
        unit_of_work_factory=create_answer_validation_unit_of_work_factory(session_factory),
        answer_generator=generator,
        support_evaluator=support,
        generation_profile=AnswerGenerationProfile(
            generator_version="m1.5-scripted-reviewed-v1",
            generation_config_version="m1.5-scripted-generation-v1",
            prompt_version=SCRIPTED_PROMPT_VERSION,
        ),
        validation_profile=EvidenceValidationProfile(
            validation_config_version=SCRIPTED_SUPPORT_VERSION
        ),
        insufficient_message=INSUFFICIENT_MESSAGE,
    )
    return AnswerResearchRequestService(retrieval_service=retrieval, generation_service=generation)


@pytest.fixture
def reviewed_corpus(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> tuple[
    EvaluationManifest,
    tuple[ResearchSource, ...],
    DeterministicRuleEmbeddingProvider,
    DeterministicSqliteDenseIndex,
]:
    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    first = make_research_source(label="M1.5 answer baseline")
    sources = tuple(
        source_for_catalog_document(document, workspace=first.workspace, context=first.context)
        for document in manifest.documents
    )
    persist_sources(integration_database_engine, sources)
    embedding = embedding_provider()
    index = DeterministicSqliteDenseIndex(tmp_path / "dense-index.sqlite")
    index_sources(sources, provider=embedding, index=index)
    return manifest, sources, embedding, index


def test_reviewed_m1_5_answer_baseline(
    integration_database_engine: Engine,
    reviewed_corpus: tuple[
        EvaluationManifest,
        tuple[ResearchSource, ...],
        DeterministicRuleEmbeddingProvider,
        DeterministicSqliteDenseIndex,
    ],
) -> None:
    manifest, sources, embedding, index = reviewed_corpus
    generator = ReviewedAnswerGenerator(manifest, sources)
    service = _make_service(
        integration_database_engine, generator, ReviewedSupportEvaluator(manifest), embedding, index
    )
    context = sources[0].context
    document_ids = dict(
        zip(
            (doc.id for doc in manifest.documents),
            (src.document.id for src in sources),
            strict=True,
        )
    )
    blocks = {block.id: block for source in sources for block in source.blocks}
    document_catalog = dict(
        zip(
            (src.document.id for src in sources),
            (doc.id for doc in manifest.documents),
            strict=True,
        )
    )
    version_catalog = dict(
        zip(
            (src.version.id for src in sources), (doc.id for doc in manifest.documents), strict=True
        )
    )
    session_factory = create_session_factory(integration_database_engine)
    observations: list[CaseObservation] = []
    for case in manifest.cases:
        started = perf_counter_ns()
        result = service.answer(
            context,
            ResearchRetrievalInput(
                question=case.question,
                document_ids=tuple(document_ids[doc_id] for doc_id in case.scope_document_ids),
            ),
        )
        assert result.answer.state in {AnswerState.STREAMED, AnswerState.INSUFFICIENT}
        generation_input = generator.inputs[-1]
        with session_factory() as session:
            research = SqlAlchemyResearchRepository(session)
            evidence = SqlAlchemyEvidenceRepository(session)
            assert research.get_answer_by_id(context, result.answer.id) == result.answer
            assert tuple(research.list_statements(context, result.answer.id)) == result.statements
            assert set(evidence.list_answer_validations(context, result.answer.id)) == set(
                result.validations
            )
            persisted_spans = []
            for span in result.evidence_spans:
                persisted = evidence.get_span_by_id(context, span.id)
                assert persisted is not None
                assert persisted == span
                persisted_spans.append(persisted)
            assert result.answer.retrieval_run_id is not None
            candidates = {
                candidate.id: candidate
                for candidate in SqlAlchemyRetrievalRepository(session).list_candidates(
                    context, result.answer.retrieval_run_id
                )
            }
        selected = []
        for source in generation_input.sources:
            candidate = candidates[source.retrieval_candidate_id]
            assert candidate.selection_rank is not None
            selected.append(
                SelectedRetrievalCandidate(
                    **source.model_dump(exclude={"retrieval_candidate_id"}),
                    origin=candidate.origin,
                    rank=candidate.rank,
                    raw_score=candidate.raw_score,
                    selection_rank=candidate.selection_rank,
                )
            )
        selected_ids = map_selected_passages(
            manifest, selected, blocks_by_id=blocks, catalog_document_ids=document_catalog
        )
        cited_ids = map_cited_passages(
            manifest,
            persisted_spans,
            blocks_by_id=blocks,
            catalog_document_version_ids=version_catalog,
        )
        observations.append(
            CaseObservation(
                case_id=case.id,
                selected_passage_ids=selected_ids,
                cited_passage_ids=cited_ids,
                citation_count=len(result.statement_evidence),
                terminal_status="COMPLETED",
                coverage_status=(
                    OBSERVED_COVERAGE[result.answer.coverage_status]
                    if result.answer.coverage_status is not None
                    else None
                ),
                exact_citations_valid=True if result.evidence_spans else None,
                support_assessment=None,
                failed_safely=None,
                elapsed_ms=(perf_counter_ns() - started) // 1_000_000,
                provider_usage=None,
            )
        )
    digest = hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest()
    observation_set = ObservationSet(
        schema_version=1,
        corpus_id=manifest.id,
        corpus_sha256=digest,
        configuration=RunConfiguration(
            lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
            embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
            dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
            selector_config_version=M1SelectionProfile().selector_config_version,
            answer_model="scripted-reviewed-cases-v1",
            support_model="scripted-reviewed-support-v1",
            answer_prompt_version=SCRIPTED_PROMPT_VERSION,
            support_prompt_version=SCRIPTED_SUPPORT_VERSION,
        ),
        run_mode="deterministic_offline",
        cases=tuple(observations),
    )
    summary = score_observations(observation_set, manifest, digest)
    assert summary.case_count == 6
    assert summary.retrieval_hit_count == summary.retrieval_case_count == 5
    assert summary.coverage_match_count == summary.citation_match_count == 6
    assert summary.exact_citation_valid_count == summary.exact_citation_checked_count == 5
    assert summary.abstention_match_count == summary.conflict_match_count == 1
    assert summary.support_assessed_count == 0
    output_path = os.environ.get(OUTPUT_PATH_ENVIRONMENT_NAME)
    if output_path:
        with Path(output_path).open("x", encoding="utf-8") as output:
            output.write(observation_set.model_dump_json(indent=2))


@pytest.mark.parametrize("failure", ("changed_quote", "rejected_support"))
def test_reviewed_m1_5_application_rejects_invalid_answers(
    integration_database_engine: Engine,
    reviewed_corpus: tuple[
        EvaluationManifest,
        tuple[ResearchSource, ...],
        DeterministicRuleEmbeddingProvider,
        DeterministicSqliteDenseIndex,
    ],
    failure: str,
) -> None:
    manifest, sources, embedding, index = reviewed_corpus
    generator = ReviewedAnswerGenerator(manifest, sources, alter_quote=failure == "changed_quote")
    support = ReviewedSupportEvaluator(manifest, reject=failure == "rejected_support")
    service = _make_service(integration_database_engine, generator, support, embedding, index)
    case = next(
        case for case in manifest.cases if case.id == "question:fy2026-battery-cabinet-window"
    )
    context = sources[0].context
    result = service.answer(
        context,
        ResearchRetrievalInput(question=case.question, document_ids=(sources[2].document.id,)),
    )
    assert result.answer.state is AnswerState.REJECTED
    assert not result.statements and not result.evidence_spans and not result.statement_evidence
    assert any(not validation.passed for validation in result.validations)
    with create_session_factory(integration_database_engine)() as session:
        research = SqlAlchemyResearchRepository(session)
        assert research.get_answer_by_id(context, result.answer.id) == result.answer
        assert not research.list_statements(context, result.answer.id)
    assert bool(support.calls) is (failure == "rejected_support")
