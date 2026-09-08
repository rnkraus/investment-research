"""Tests for the generated backend-to-frontend API contract."""

import json
from pathlib import Path

from investment_research.main import create_app
from investment_research.settings import Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
OPENAPI_SNAPSHOT = REPOSITORY_ROOT / "frontend/src/lib/api/generated/openapi.json"


def test_committed_openapi_snapshot_matches_application() -> None:
    application_schema = create_app(Settings(environment="test")).openapi()
    committed_schema = json.loads(OPENAPI_SNAPSHOT.read_text())

    assert committed_schema == application_schema


def test_openapi_does_not_advertise_unimplemented_product_routes() -> None:
    schema = create_app(Settings(environment="test")).openapi()

    assert set(schema["paths"]) == {"/health"}


def test_research_input_schema_has_no_client_workspace_scope() -> None:
    schema = create_app(Settings(environment="test")).openapi()
    research_input = schema["components"]["schemas"]["ResearchRequestInput"]

    assert "workspace_id" not in json.dumps(research_input)


def test_research_context_schema_defaults_to_current_temporal_mode() -> None:
    schema = create_app(Settings(environment="test")).openapi()
    research_context = schema["components"]["schemas"]["ResearchRequestContext"]
    temporal_mode = schema["components"]["schemas"]["TemporalMode"]

    assert research_context["properties"]["temporal_mode"]["default"] == "CURRENT"
    assert set(temporal_mode["enum"]) == {
        "CURRENT",
        "PUBLIC_KNOWLEDGE",
        "PERSONAL_ARCHIVE",
    }


def test_openapi_exposes_discriminated_research_stream_union() -> None:
    schema = create_app(Settings(environment="test")).openapi()
    stream_event = schema["components"]["schemas"]["ResearchStreamEvent"]

    assert stream_event["discriminator"]["propertyName"] == "event"
    assert set(stream_event["discriminator"]["mapping"]) == {
        "research.progress",
        "answer.statement",
        "answer.citation",
        "answer.completed",
        "answer.insufficient",
        "research.failed",
    }
    assert len(stream_event["oneOf"]) == 6
