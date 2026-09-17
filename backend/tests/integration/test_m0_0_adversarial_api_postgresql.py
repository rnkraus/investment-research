"""Adversarial PostgreSQL API proofs for untrusted generated answer content."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from investment_research.api.dependencies import (
    get_answer_research_request_service,
    get_workspace_context,
)
from investment_research.bootstrap import (
    M0_0_EVIDENCE_VALIDATION_PROFILE,
    M0_0_GENERATION_PROFILE,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.infrastructure.database.unit_of_work import (
    create_answer_validation_unit_of_work_factory,
)
from investment_research.infrastructure.evidence.deterministic import (
    DeterministicFixtureStatementSupportEvaluator,
    FixtureStatementSupportRule,
)
from investment_research.infrastructure.generation.deterministic import (
    M0_0_FIXTURE_ANSWER_CASE,
    M0_0_INSUFFICIENT_MESSAGE,
)
from investment_research.main import create_app
from investment_research.modules.evidence.persistence import EvidenceSpanRow
from investment_research.modules.processing.application import M1_PROCESSING_PROFILE
from investment_research.modules.research.application import (
    AnswerResearchRequestService,
    GenerateValidatedAnswerService,
)
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    UnvalidatedEvidenceReference,
    UnvalidatedGeneratedAnswer,
    UnvalidatedGeneratedStatement,
)
from investment_research.modules.research.domain import AnswerState, CoverageStatus
from investment_research.modules.research.persistence import AnswerRow, AnswerStatementRow
from investment_research.settings import ApiSettings
from tests.integration.factories import (
    make_searchable_research_source,
    persist_research_source,
)

type AdversarialCase = Literal["unsupported-statement", "tampered-citation"]


@dataclass(frozen=True, slots=True)
class AdversarialAnswerGenerator:
    """Return one schema-valid proposal that canonical validation must reject."""

    case: AdversarialCase

    def generate(self, generation_input: AnswerGenerationInput) -> UnvalidatedGeneratedAnswer:
        source = generation_input.sources[0]
        expected_text = M0_0_FIXTURE_ANSWER_CASE.evidence_text
        start_character = source.text.index(expected_text)
        proposed_text = (
            "X" + expected_text[1:] if self.case == "tampered-citation" else expected_text
        )
        statement = (
            "Northstar Grid Analytics reported a severe FY2025 revenue decline."
            if self.case == "unsupported-statement"
            else M0_0_FIXTURE_ANSWER_CASE.statement
        )
        return UnvalidatedGeneratedAnswer(
            coverage_status=CoverageStatus.COMPLETE,
            rendered_text="Untrusted answer content must not reach the API.",
            statements=(
                UnvalidatedGeneratedStatement(
                    text=statement,
                    evidence=(
                        UnvalidatedEvidenceReference(
                            retrieval_candidate_id=source.retrieval_candidate_id,
                            start_character=start_character,
                            end_character=start_character + len(proposed_text),
                            exact_text=proposed_text,
                        ),
                    ),
                ),
            ),
        )


def parse_server_sent_events(body: str) -> list[dict[str, Any]]:
    """Decode a complete deterministic server-sent-event response."""

    events: list[dict[str, Any]] = []
    for frame in body.strip().split("\n\n"):
        lines = frame.splitlines()
        events.append(
            {
                "event": lines[0].removeprefix("event: "),
                "data": json.loads(lines[1].removeprefix("data: ")),
            }
        )
    return events


@pytest.mark.parametrize(
    "adversarial_case",
    [
        pytest.param("unsupported-statement", id="invalid-output"),
        pytest.param("tampered-citation", id="tampered-citation"),
    ],
)
def test_api_rejects_untrusted_answer_without_exposing_usable_content(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    tmp_path: Path,
    adversarial_case: AdversarialCase,
) -> None:
    """Prove the real validator fails closed before API answer events exist."""

    searchable_text = (
        f"{M0_0_FIXTURE_ANSWER_CASE.question} {M0_0_FIXTURE_ANSWER_CASE.evidence_text}"
    )
    source = make_searchable_research_source(
        label=f"M0.0 Adversarial API {adversarial_case}",
        primary_text=searchable_text,
        lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
    )
    persist_research_source(integration_database_engine, source)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: source.context

    with TestClient(application) as client:
        container = application.state.container
        generation_service = GenerateValidatedAnswerService(
            unit_of_work_factory=create_answer_validation_unit_of_work_factory(
                container.session_factory
            ),
            answer_generator=AdversarialAnswerGenerator(adversarial_case),
            support_evaluator=DeterministicFixtureStatementSupportEvaluator(
                [
                    FixtureStatementSupportRule(
                        statement=M0_0_FIXTURE_ANSWER_CASE.statement,
                        evidence_text=M0_0_FIXTURE_ANSWER_CASE.evidence_text,
                    )
                ]
            ),
            generation_profile=M0_0_GENERATION_PROFILE,
            validation_profile=M0_0_EVIDENCE_VALIDATION_PROFILE,
            insufficient_message=M0_0_INSUFFICIENT_MESSAGE,
        )
        service = AnswerResearchRequestService(
            retrieval_service=container.retrieve_research_service,
            generation_service=generation_service,
        )
        application.dependency_overrides[get_answer_research_request_service] = lambda: service

        response = client.post(
            "/api/v1/research-requests",
            json={
                "question": M0_0_FIXTURE_ANSWER_CASE.question,
                "context": {"document_ids": [str(source.document.id)]},
            },
        )

    assert response.status_code == 200
    events = parse_server_sent_events(response.text)
    assert [event["event"] for event in events] == ["research.failed"]
    assert events[0]["data"]["error"]["code"] == "EVIDENCE_VALIDATION_FAILED"
    assert events[0]["data"]["retriable"] is False
    assert "Untrusted answer content" not in response.text
    assert "answer.statement" not in response.text
    assert "answer.citation" not in response.text

    session_factory = create_session_factory(integration_database_engine)
    with session_factory() as session:
        answers = tuple(
            session.scalars(
                select(AnswerRow).where(AnswerRow.workspace_id == source.context.workspace_id)
            )
        )
        statement_count = session.scalar(
            select(func.count())
            .select_from(AnswerStatementRow)
            .where(AnswerStatementRow.workspace_id == source.context.workspace_id)
        )
        evidence_count = session.scalar(
            select(func.count())
            .select_from(EvidenceSpanRow)
            .where(EvidenceSpanRow.workspace_id == source.context.workspace_id)
        )

    assert len(answers) == 1
    assert answers[0].state is AnswerState.REJECTED
    assert answers[0].rendered_text is None
    assert statement_count == 0
    assert evidence_count == 0
