"""OpenAPI generation with explicit registration of planned M0.0 contracts."""

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import TypeAdapter

from investment_research.api.contracts import PUBLIC_CONTRACT_MODELS, ResearchStreamEvent

JsonObject = dict[str, Any]


def _without_null_defaults(value: Any) -> Any:
    """Normalize FastAPI's omission of semantically optional null defaults."""

    if isinstance(value, Mapping):
        return {
            key: _without_null_defaults(item)
            for key, item in value.items()
            if key != "default" or item is not None
        }
    if isinstance(value, list):
        return [_without_null_defaults(item) for item in value]
    return value


def _merge_schema(target: JsonObject, name: str, schema: Mapping[str, Any]) -> None:
    """Add one schema while rejecting incompatible duplicate definitions."""

    existing = target.get(name)
    candidate = dict(schema)
    if existing is not None:
        if _without_null_defaults(existing) != _without_null_defaults(candidate):
            raise RuntimeError(f"conflicting OpenAPI schema definition: {name}")
        return
    target[name] = candidate


def _merge_schema_with_definitions(
    target: JsonObject,
    name: str,
    schema: Mapping[str, Any],
) -> None:
    """Add a named root schema and its reusable definitions."""

    root_schema = dict(schema)
    definitions = root_schema.pop("$defs", {})
    for definition_name, definition in definitions.items():
        _merge_schema(target, definition_name, definition)
    _merge_schema(target, name, root_schema)


def _contract_schemas() -> JsonObject:
    """Build reusable OpenAPI components from the Pydantic source models."""

    schemas: JsonObject = {}
    for model in PUBLIC_CONTRACT_MODELS:
        model_schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        _merge_schema_with_definitions(schemas, model.__name__, model_schema)

    stream_schema = TypeAdapter(ResearchStreamEvent).json_schema(
        ref_template="#/components/schemas/{model}"
    )
    _merge_schema_with_definitions(schemas, "ResearchStreamEvent", stream_schema)
    return dict(sorted(schemas.items()))


def install_openapi(application: FastAPI) -> None:
    """Install deterministic OpenAPI generation for routes and contract components."""

    def generate_openapi() -> JsonObject:
        if application.openapi_schema is not None:
            return application.openapi_schema

        schema = get_openapi(
            title=application.title,
            version=application.version,
            description=application.description,
            routes=application.routes,
        )
        components = schema.setdefault("components", {})
        schemas = components.setdefault("schemas", {})
        for name, contract_schema in _contract_schemas().items():
            _merge_schema(schemas, name, contract_schema)

        application.openapi_schema = schema
        return schema

    application.openapi = generate_openapi  # type: ignore[method-assign]
