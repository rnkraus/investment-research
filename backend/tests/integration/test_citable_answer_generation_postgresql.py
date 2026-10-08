"""Canonical source-ID answers with PostgreSQL and a simulated, zero-spend SDK."""

from collections.abc import Sequence
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select, update

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.infrastructure.database.unit_of_work import (
    create_answer_validation_unit_of_work_factory,
    create_citable_source_unit_of_work_factory,
    create_evidence_read_unit_of_work_factory,
)
from investment_research.infrastructure.generation.openai_citable_units import (
    citable_answer_profile,
)
from investment_research.modules.documents.domain import OriginalMediaType, SourceLocatorKind
from investment_research.modules.evidence.application import (
    EvidenceSpanUnavailableError,
    ResolveEvidenceSpanService,
)
from investment_research.modules.evidence.contracts import (
    EvidenceValidationProfile,
    StatementSupportDecision,
    StatementSupportResult,
)
from investment_research.modules.evidence.persistence import EvidenceSpanRow
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.processing.persistence import ChunkSetRow
from investment_research.modules.research.answer_validation import GenerateValidatedAnswerService
from investment_research.modules.research.citable_generation import CitableGenerationInputBuilder
from investment_research.modules.research.citable_unit_contracts import CitableUnitError
from investment_research.modules.research.contracts import (
    CanonicalCandidateSource,
    ResearchModel,
    ResearchRetrievalResult,
)
from investment_research.modules.research.domain import AnswerState
from investment_research.modules.research.persistence import SqlAlchemyResearchRepository
from tests.integration.factories import (
    ResearchSource,
    make_research_source,
    make_retrieval_result,
    persist_research_source,
    persist_retrieval_result,
)
from tests.unit.test_openai_citable_unit_answer_generator import RecordingClient, make_generator

TEXT = "Café 😀 revenue was $3 million. Café 😀 revenue was $3 million."


def prepare(engine: Engine, *, pasted: bool) -> tuple[ResearchSource, ResearchRetrievalResult]:
    source = make_research_source(label="Citable synthetic source")
    texts = (TEXT, "The rollout requires transformers.")
    blocks = tuple(
        block.model_copy(
            update={
                "text": text,
                "source_locator_kind": SourceLocatorKind.TEXT_PASSAGE
                if pasted
                else SourceLocatorKind.PDF_PAGE,
                "page_number": None if pasted else block.page_number,
                "start_character": 40 + ordinal * 100 if pasted else 0,
                "end_character": (40 + ordinal * 100 if pasted else 0) + len(text),
            }
        )
        for ordinal, (block, text) in enumerate(zip(source.blocks, texts, strict=True))
    )
    source = replace(
        source,
        blocks=blocks,
        original=source.original.model_copy(
            update={"media_type": OriginalMediaType.PLAIN_TEXT if pasted else OriginalMediaType.PDF}
        ),
        chunks=tuple(
            chunk.model_copy(update={"text": block.text})
            for chunk, block in zip(source.chunks, blocks, strict=True)
        ),
    )
    persist_research_source(engine, source)
    return source, persist_selection(engine, source)


def persist_selection(engine: Engine, source: ResearchSource) -> ResearchRetrievalResult:
    retrieval = make_retrieval_result(source)
    persist_retrieval_result(engine, source, retrieval)
    return ResearchRetrievalResult(
        request=source.request,
        retrieval_run=retrieval.run,
        candidates=retrieval.candidates,
        sources=tuple(
            CanonicalCandidateSource(
                retrieval_candidate_id=candidate.id,
                chunk_id=chunk.id,
                document_id=source.document.id,
                document_version_id=source.version.id,
                structure_block_id=block.id,
                source_locator_kind=block.source_locator_kind,
                page_number=block.page_number,
                text=block.text,
            )
            for candidate, chunk, block in zip(
                retrieval.candidates, source.chunks, source.blocks, strict=True
            )
        ),
    )


class Support:
    def __init__(self, engine: Engine, source: ResearchSource, *, deactivate: bool = False) -> None:
        self.engine, self.source, self.deactivate = engine, source, deactivate
        self.calls = 0

    def evaluate(self, statement: str, evidence_texts: Sequence[str]) -> StatementSupportResult:
        self.calls += 1
        assert evidence_texts
        if self.deactivate:
            with transactional_session(create_session_factory(self.engine)) as session:
                session.execute(
                    update(ChunkSetRow)
                    .where(ChunkSetRow.id == self.source.chunk_set.id)
                    .values(is_active=False)
                )
        return StatementSupportResult(
            decision=StatementSupportDecision(supported=True),
            usage=ProviderUsage(input_units=10, output_units=1),
        )


def make_service(
    engine: Engine,
    source: ResearchSource,
    selection: ResearchRetrievalResult,
    *,
    two: bool = False,
    deactivate: bool = False,
) -> tuple[GenerateValidatedAnswerService, RecordingClient, Support]:
    sessions = create_session_factory(engine)
    builder = CitableGenerationInputBuilder(create_citable_source_unit_of_work_factory(sessions))
    request = builder.build(source.context, selection)
    units = (
        (request.catalog.units[1], request.catalog.units[-1])
        if two
        else (request.catalog.units[1],)
    )
    statement = " ".join(unit.text for unit in units)
    generator, client = make_generator(
        request,
        payload={
            "coverage_status": "COMPLETE",
            "rendered_text": statement,
            "statements": [
                {
                    "text": statement,
                    "source_unit_ids": [
                        f"u{request.catalog.units.index(unit) + 1}" for unit in units
                    ],
                }
            ],
            "missing_information": None,
        },
    )
    support = Support(engine, source, deactivate=deactivate)
    service = GenerateValidatedAnswerService(
        unit_of_work_factory=create_answer_validation_unit_of_work_factory(sessions),
        generation_input_builder=builder,
        answer_generator=generator,
        support_evaluator=support,
        generation_profile=citable_answer_profile(ResearchModel.TERRA),
        validation_profile=EvidenceValidationProfile(
            validation_config_version="m1-evidence-validation-v1"
        ),
        insufficient_message="The archive does not contain sufficient evidence.",
    )
    return service, client, support


@pytest.mark.parametrize("pasted", [False, True])
@pytest.mark.parametrize("two", [False, True])
def test_id_selection_round_trips_exact_occurrence_and_original_locators(
    integration_database_engine: Engine,
    pasted: bool,
    two: bool,
) -> None:
    engine = integration_database_engine
    source, selection = prepare(engine, pasted=pasted)
    service, client, support = make_service(engine, source, selection, two=two)
    result = service.generate(source.context, selection)
    assert result.answer.state is AnswerState.STREAMED
    assert len(client.responses.calls) == support.calls == 1
    assert len(result.evidence_spans) == (2 if two else 1)
    assert result.evidence_spans[0].start_character == TEXT.rfind("Café")
    resolver = ResolveEvidenceSpanService(
        unit_of_work_factory=create_evidence_read_unit_of_work_factory(
            create_session_factory(engine)
        )
    )
    for span in result.evidence_spans:
        resolved = resolver.resolve(source.context, span.id)
        assert resolved is not None and resolved.span == span
        if pasted:
            assert span.page_number is None
            block = next(block for block in source.blocks if block.id == span.structure_block_id)
            assert span.source_start_character == block.start_character + span.start_character
        else:
            assert span.page_number is not None and span.source_start_character is None
        with pytest.raises(EvidenceSpanUnavailableError):
            resolver.resolve(WorkspaceContext(user_id=uuid4(), workspace_id=uuid4()), span.id)


def test_repeated_answers_reuse_canonical_spans_with_new_request_handles(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    source, selection = prepare(engine, pasted=True)
    first, _, _ = make_service(engine, source, selection)
    result = first.generate(source.context, selection)
    second_source = replace(source, request=source.request.model_copy(update={"id": uuid4()}))
    with transactional_session(create_session_factory(engine)) as session:
        SqlAlchemyResearchRepository(session).add_request(source.context, second_source.request)
    second_selection = persist_selection(engine, second_source)
    second, _, _ = make_service(engine, second_source, second_selection)
    repeated = second.generate(source.context, second_selection)
    assert repeated.answer.state is AnswerState.STREAMED
    assert repeated.evidence_spans[0].id == result.evidence_spans[0].id
    with create_session_factory(engine)() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(EvidenceSpanRow)
                .where(EvidenceSpanRow.workspace_id == source.context.workspace_id)
            )
            == 1
        )


def test_source_changed_during_support_is_rejected_at_final_check(
    integration_database_engine: Engine,
) -> None:
    source, selection = prepare(integration_database_engine, pasted=False)
    service, client, support = make_service(
        integration_database_engine, source, selection, deactivate=True
    )
    result = service.generate(source.context, selection)
    assert len(client.responses.calls) == support.calls == 1
    assert result.answer.state is AnswerState.REJECTED
    assert not result.evidence_spans and not result.statements


def test_stale_source_rejected_before_generator(integration_database_engine: Engine) -> None:
    engine = integration_database_engine
    source, selection = prepare(engine, pasted=False)
    service, client, support = make_service(engine, source, selection)
    with transactional_session(create_session_factory(engine)) as session:
        session.execute(
            update(ChunkSetRow).where(ChunkSetRow.id == source.chunk_set.id).values(is_active=False)
        )
    with pytest.raises(CitableUnitError):
        service.generate(source.context, selection)
    assert not client.responses.calls and support.calls == 0


def test_empty_selection_persists_insufficient_without_model_calls(
    integration_database_engine: Engine,
) -> None:
    source, selection = prepare(integration_database_engine, pasted=False)
    service, client, support = make_service(integration_database_engine, source, selection)
    empty = selection.model_copy(update={"sources": (), "candidates": ()})
    result = service.generate(source.context, empty)
    assert result.answer.state is AnswerState.INSUFFICIENT
    assert not client.responses.calls and support.calls == 0
