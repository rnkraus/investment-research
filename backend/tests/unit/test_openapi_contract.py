"""Tests for the generated backend-to-frontend API contract."""

import json
from pathlib import Path
from typing import Any

import pytest

from investment_research.api.openapi import _merge_schema
from investment_research.main import create_app
from investment_research.settings import ApiSettings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
OPENAPI_SNAPSHOT = REPOSITORY_ROOT / "frontend/src/lib/api/generated/openapi.json"


def test_committed_openapi_snapshot_matches_application() -> None:
    application_schema = create_app(ApiSettings(environment="test")).openapi()
    committed_schema = json.loads(OPENAPI_SNAPSHOT.read_text())

    assert committed_schema == application_schema


def test_openapi_advertises_only_implemented_product_routes() -> None:
    schema = create_app(ApiSettings(environment="test")).openapi()

    assert set(schema["paths"]) == {
        "/health",
        "/ready",
        "/api/v1/documents",
        "/api/v1/documents/text",
        "/api/v1/documents/deletions",
        "/api/v1/documents/{document_id}/deletion",
        "/api/v1/documents/{document_id}/versions/{version_id}/original",
        "/api/v1/evidence-spans/{evidence_span_id}",
        "/api/v1/processing-runs/{processing_run_id}",
        "/api/v1/research-requests",
        "/api/v1/research-requests/models",
    }


def test_readiness_exposes_only_the_aggregate_state() -> None:
    schema = create_app(ApiSettings(environment="test")).openapi()
    operation = schema["paths"]["/ready"]["get"]

    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadinessResponse"
    }
    assert operation["responses"]["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadinessResponse"
    }
    assert "database" not in json.dumps(operation)
    assert "postgresql" not in json.dumps(operation)


def test_document_capture_uses_scoped_multipart_contract() -> None:
    schema = create_app(ApiSettings(environment="test")).openapi()
    operation = schema["paths"]["/api/v1/documents"]["post"]
    body_reference = operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    body_name = body_reference.rsplit("/", maxsplit=1)[-1]
    body_schema = schema["components"]["schemas"][body_name]

    assert set(body_schema["properties"]) == {
        "file",
        "title",
        "language",
        "published_at",
        "available_at",
    }
    assert set(body_schema["required"]) == {"file", "title"}
    assert "workspace_id" not in json.dumps(operation)
    assert operation["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DocumentAccepted"
    }
    for status_code in ("409", "413", "415", "422", "500"):
        assert operation["responses"][status_code]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }


def test_document_catalog_exposes_bounded_pagination_without_workspace_input() -> None:
    schema = create_app(ApiSettings(environment="test")).openapi()
    operation = schema["paths"]["/api/v1/documents"]["get"]

    assert {parameter["name"] for parameter in operation["parameters"]} == {
        "page_size",
        "after_created_at",
        "after_document_id",
    }
    size_schema = next(
        parameter["schema"]
        for parameter in operation["parameters"]
        if parameter["name"] == "page_size"
    )
    assert size_schema["minimum"] == 1
    assert size_schema["maximum"] == 100
    assert size_schema["default"] == 20
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DocumentCatalogPage"
    }
    assert (
        schema["components"]["schemas"]["DocumentCatalogPage"]["properties"]["items"]["maxItems"]
        == 100
    )
    for status_code in ("422", "500"):
        assert operation["responses"][status_code]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
    assert "workspace_id" not in json.dumps(operation)


def test_deletion_contract_requires_explicit_confirmation_and_exposes_no_content() -> None:
    schema = create_app(ApiSettings(environment="test")).openapi()
    operation = schema["paths"]["/api/v1/documents/deletions"]["post"]
    body = schema["components"]["schemas"]["DocumentDeletionInput"]
    assert body["additionalProperties"] is False
    assert set(body["required"]) == {"document_ids", "confirm_permanent"}
    assert body["properties"]["confirm_permanent"]["const"] is True
    assert body["properties"]["document_ids"]["minItems"] == 1
    assert body["properties"]["document_ids"]["maxItems"] == 100
    assert operation["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DocumentDeletionsAccepted"
    }
    result = schema["components"]["schemas"]["DocumentDeletionResult"]
    assert set(result["properties"]) == {
        "document_id",
        "state",
        "requested_at",
        "completed_at",
        "status_url",
    }
    assert set(schema["components"]["schemas"]["DocumentDeletionState"]["enum"]) == {
        "PENDING",
        "COMPLETED",
    }
    for code in ("404", "409", "422", "500"):
        assert operation["responses"][code]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }


def test_text_capture_uses_strict_bounded_json_contract() -> None:
    schema = create_app(ApiSettings(environment="test")).openapi()
    operation = schema["paths"]["/api/v1/documents/text"]["post"]

    assert operation["requestBody"]["content"] == {
        "application/json": {"schema": {"$ref": "#/components/schemas/PastedTextDocumentInput"}}
    }
    body_schema = schema["components"]["schemas"]["PastedTextDocumentInput"]
    assert body_schema["additionalProperties"] is False
    assert set(body_schema["required"]) == {"title", "text"}
    assert body_schema["properties"]["language"]["const"] == "en"
    assert body_schema["properties"]["text"]["maxLength"] == 100_000
    assert "workspace_id" not in json.dumps(operation)
    for status_code in ("409", "413", "422", "500"):
        assert operation["responses"][status_code]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }


def test_original_route_exposes_media_aware_original_without_workspace_input() -> None:
    schema = create_app(ApiSettings(environment="test")).openapi()
    operation = schema["paths"]["/api/v1/documents/{document_id}/versions/{version_id}/original"][
        "get"
    ]

    assert operation["parameters"] == [
        {
            "name": "document_id",
            "in": "path",
            "required": True,
            "schema": {"type": "string", "format": "uuid", "title": "Document Id"},
        },
        {
            "name": "version_id",
            "in": "path",
            "required": True,
            "schema": {"type": "string", "format": "uuid", "title": "Version Id"},
        },
    ]
    assert "workspace_id" not in json.dumps(operation)
    assert operation["responses"]["200"]["content"] == {
        "application/pdf": {"schema": {"type": "string", "format": "binary"}},
        "text/plain": {"schema": {"type": "string"}},
    }
    for status_code in ("404", "422", "500"):
        assert operation["responses"][status_code]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }


def test_processing_status_exposes_safe_scoped_result_without_workspace_input() -> None:
    schema = create_app(ApiSettings(environment="test")).openapi()
    operation = schema["paths"]["/api/v1/processing-runs/{processing_run_id}"]["get"]

    assert operation["parameters"] == [
        {
            "name": "processing_run_id",
            "in": "path",
            "required": True,
            "schema": {
                "type": "string",
                "format": "uuid",
                "title": "Processing Run Id",
            },
        }
    ]
    assert "workspace_id" not in json.dumps(operation)
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProcessingRunResult"
    }
    for status_code in ("404", "422", "500"):
        assert operation["responses"][status_code]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }


def test_evidence_route_exposes_canonical_location_without_workspace_input() -> None:
    schema = create_app(ApiSettings(environment="test")).openapi()
    operation = schema["paths"]["/api/v1/evidence-spans/{evidence_span_id}"]["get"]

    assert operation["parameters"] == [
        {
            "name": "evidence_span_id",
            "in": "path",
            "required": True,
            "schema": {
                "type": "string",
                "format": "uuid",
                "title": "Evidence Span Id",
            },
        }
    ]
    assert "workspace_id" not in json.dumps(operation)
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EvidenceSpanResult"
    }
    for status_code in ("404", "422", "500"):
        assert operation["responses"][status_code]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }


def test_research_route_exposes_scoped_input_and_typed_event_stream() -> None:
    schema = create_app(ApiSettings(environment="test")).openapi()
    operation = schema["paths"]["/api/v1/research-requests"]["post"]

    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ResearchRequestInput"
    }
    assert "workspace_id" not in json.dumps(operation)
    assert operation["responses"]["200"]["content"]["text/event-stream"]["schema"] == {
        "$ref": "#/components/schemas/ResearchStreamEvent"
    }
    for status_code in ("422", "500"):
        assert operation["responses"][status_code]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }


def test_schema_merge_accepts_only_null_default_representation_difference() -> None:
    target: dict[str, Any] = {
        "Example": {
            "type": "object",
            "properties": {"optional": {"type": ["string", "null"]}},
        }
    }

    _merge_schema(
        target,
        "Example",
        {
            "type": "object",
            "properties": {"optional": {"type": ["string", "null"], "default": None}},
        },
    )

    assert "default" not in target["Example"]["properties"]["optional"]


def test_schema_merge_still_rejects_real_definition_conflict() -> None:
    target = {"Example": {"type": "string"}}

    with pytest.raises(RuntimeError, match="conflicting OpenAPI schema"):
        _merge_schema(target, "Example", {"type": "integer"})


def test_research_input_schema_has_no_client_workspace_scope() -> None:
    schema = create_app(ApiSettings(environment="test")).openapi()
    research_input = schema["components"]["schemas"]["ResearchRequestInput"]

    assert "workspace_id" not in json.dumps(research_input)


def test_research_context_schema_defaults_to_current_temporal_mode() -> None:
    schema = create_app(ApiSettings(environment="test")).openapi()
    research_context = schema["components"]["schemas"]["ResearchRequestContext"]
    temporal_mode = schema["components"]["schemas"]["TemporalMode"]

    assert research_context["properties"]["temporal_mode"]["default"] == "CURRENT"
    assert set(temporal_mode["enum"]) == {
        "CURRENT",
        "PUBLIC_KNOWLEDGE",
        "PERSONAL_ARCHIVE",
    }


def test_openapi_exposes_discriminated_research_stream_union() -> None:
    schema = create_app(ApiSettings(environment="test")).openapi()
    stream_event = schema["components"]["schemas"]["ResearchStreamEvent"]

    assert stream_event["discriminator"]["propertyName"] == "event"
    assert set(stream_event["discriminator"]["mapping"]) == {
        "research.context",
        "research.progress",
        "answer.statement",
        "answer.citation",
        "answer.completed",
        "answer.insufficient",
        "research.failed",
    }
    assert len(stream_event["oneOf"]) == 7
