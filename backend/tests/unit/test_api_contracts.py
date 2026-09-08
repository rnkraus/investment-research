"""Tests for strict, versioned M0.0 API contracts."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from investment_research.api.contracts import (
    DocumentMetadataInput,
    EvidenceSpanResult,
    ResearchRequestContext,
    ResearchRequestInput,
    ResearchStreamEvent,
)
from investment_research.modules.research.domain import TemporalMode


def test_research_request_rejects_client_workspace_scope() -> None:
    with pytest.raises(ValidationError, match="workspace_id"):
        ResearchRequestInput.model_validate(
            {
                "question": "What did management report?",
                "workspace_id": "00000000-0000-4000-8000-000000000099",
            }
        )


def test_research_request_defaults_to_current_archive_state() -> None:
    request = ResearchRequestInput(question="What did management report?")

    assert request.context == ResearchRequestContext(
        temporal_mode=TemporalMode.CURRENT,
        cutoff_at=None,
    )


@pytest.mark.parametrize(
    "temporal_mode",
    [TemporalMode.PUBLIC_KNOWLEDGE, TemporalMode.PERSONAL_ARCHIVE],
)
def test_historical_research_request_requires_cutoff(temporal_mode: TemporalMode) -> None:
    with pytest.raises(ValidationError, match="historical temporal mode requires cutoff_at"):
        ResearchRequestContext(temporal_mode=temporal_mode)


def test_current_research_request_rejects_cutoff() -> None:
    with pytest.raises(ValidationError, match="CURRENT temporal mode must not define cutoff_at"):
        ResearchRequestContext(
            temporal_mode=TemporalMode.CURRENT,
            cutoff_at=datetime(2025, 12, 31, tzinfo=UTC),
        )


def test_historical_research_request_accepts_explicit_cutoff() -> None:
    cutoff = datetime(2025, 12, 31, tzinfo=UTC)

    context = ResearchRequestContext(
        temporal_mode=TemporalMode.PUBLIC_KNOWLEDGE,
        cutoff_at=cutoff,
    )

    assert context.cutoff_at == cutoff


def test_document_language_metadata_is_extensible_beyond_english() -> None:
    metadata = DocumentMetadataInput(title=" Annual report ", language="de-DE")

    assert metadata.title == "Annual report"
    assert metadata.language == "de-DE"


def test_evidence_span_requires_an_ordered_source_range() -> None:
    with pytest.raises(ValidationError, match="end_character"):
        EvidenceSpanResult(
            evidence_span_id=UUID("00000000-0000-4000-8000-000000000010"),
            document_id=UUID("00000000-0000-4000-8000-000000000011"),
            document_version_id=UUID("00000000-0000-4000-8000-000000000012"),
            page=1,
            start_character=20,
            end_character=20,
            exact_text="Revenue increased.",
            text_sha256="a" * 64,
            viewer_url="/documents/00000000-0000-4000-8000-000000000011?page=1",
        )


def test_stream_event_discriminator_rejects_unknown_event_types() -> None:
    adapter: TypeAdapter[ResearchStreamEvent] = TypeAdapter(ResearchStreamEvent)

    with pytest.raises(ValidationError, match="event"):
        adapter.validate_python(
            {
                "schema_version": "1.0",
                "research_request_id": "00000000-0000-4000-8000-000000000020",
                "event": "answer.raw-token",
            }
        )
