"""Offline tests for ID selection, exact resolution, and the retained provider boundary."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from httpx2 import Request, Response
from openai import APIConnectionError, APIStatusError, OpenAI
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, ValidationError

from investment_research.infrastructure.generation import openai_citable_units as adapter
from investment_research.infrastructure.generation.openai import (
    M1_ANSWER_GENERATOR_VERSION,
    M1_ANSWER_PROMPT_VERSION,
    OPENAI_ANSWER_MODEL,
    OpenAIAnswerProviderConfigurationError,
    OpenAIAnswerProviderContractError,
)
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.processing.domain import StructureBlock, StructureBlockType
from investment_research.modules.research.citable_unit_contracts import (
    CitableAnswerGenerationInput,
    CitableUnitError,
    CitableUnitLimitError,
    CitableUnitSource,
)
from investment_research.modules.research.citable_units import build_citable_unit_catalog
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    CanonicalCandidateSource,
    ResearchModel,
)
from investment_research.modules.research.domain import CoverageStatus
from investment_research.modules.research.ports import (
    AnswerGenerationProviderOutputLimitError,
    AnswerGenerationProviderRefusalError,
    AnswerGenerationProviderTransientError,
)

CONTEXT = WorkspaceContext(user_id=UUID(int=1), workspace_id=UUID(int=2))
REQUEST_ID = UUID(int=3)
SOURCE_TEXT = "The pilot budget is $3 million. The pilot budget is $3 million."
STATEMENT = "The pilot budget is $3 million."


def make_input(
    *, text: str = SOURCE_TEXT, request_id: UUID = REQUEST_ID, empty: bool = False
) -> CitableAnswerGenerationInput:
    """Build independently owned synthetic block snapshots and their scoped catalog."""

    block = StructureBlock(
        id=UUID(int=4),
        workspace_id=CONTEXT.workspace_id,
        processing_run_id=UUID(int=5),
        document_version_id=UUID(int=6),
        source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
        page_number=None,
        block_type=StructureBlockType.PARAGRAPH,
        ordinal=0,
        text=text,
        start_character=40,
        end_character=40 + len(text),
        created_at=datetime(2026, 10, 7, tzinfo=UTC),
    )
    source = CitableUnitSource(
        block=block,
        source=CanonicalCandidateSource(
            retrieval_candidate_id=UUID(int=7),
            chunk_id=UUID(int=8),
            document_id=UUID(int=9),
            document_version_id=block.document_version_id,
            structure_block_id=block.id,
            source_locator_kind=block.source_locator_kind,
            text=text,
        ),
    )
    catalog = build_citable_unit_catalog(
        context=CONTEXT, research_request_id=request_id, sources=() if empty else (source,)
    )
    return CitableAnswerGenerationInput.from_catalog(
        context=CONTEXT, question="What is the pilot budget?", catalog=catalog
    )


def make_payload(
    generation_input: CitableAnswerGenerationInput,
    *,
    ids: tuple[UUID, ...] | None = None,
    coverage: CoverageStatus = CoverageStatus.COMPLETE,
    missing: str | None = None,
) -> dict[str, object]:
    """Return only the new provider fields, not quotes or character positions."""

    selected = ids if ids is not None else (generation_input.catalog.units[-1].source_unit_id,)
    return {
        "coverage_status": coverage.value,
        "rendered_text": STATEMENT,
        "statements": []
        if coverage is CoverageStatus.INSUFFICIENT
        else [{"text": STATEMENT, "source_unit_ids": [str(identifier) for identifier in selected]}],
        "missing_information": missing,
    }


@dataclass
class RawResponse:
    resource: RecordingResponses
    schema: type[BaseModel]

    @property
    def http_response(self) -> RawResponse:
        return self

    def json(self) -> object:
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(self.resource.payload)}],
                }
            ]
        }

    def parse(self) -> object:
        return self.resource.parsed(self.schema)


@dataclass
class RecordingResponses:
    payload: dict[str, object]
    error: Exception | None = None
    overrides: dict[str, object] = field(default_factory=dict)
    calls: list[dict[str, object]] = field(default_factory=list)
    raw_route: bool = False

    @property
    def with_raw_response(self) -> RecordingResponses:
        self.raw_route = True
        return self

    def parsed(self, schema: type[BaseModel]) -> object:
        values = {
            "model": OPENAI_ANSWER_MODEL,
            "status": "completed",
            "output_parsed": schema.model_validate(self.payload),
            "output": (),
            "incomplete_details": None,
            "usage": SimpleNamespace(input_tokens=120, output_tokens=30, total_tokens=150),
        }
        values.update(self.overrides)
        return SimpleNamespace(**values)

    def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        schema = cast(type[BaseModel], kwargs["text_format"])
        return RawResponse(self, schema) if self.raw_route else self.parsed(schema)


@dataclass
class RecordingClient:
    responses: RecordingResponses
    closed: bool = False

    def close(self) -> None:
        self.closed = True


def make_generator(
    generation_input: CitableAnswerGenerationInput,
    *,
    payload: dict[str, object] | None = None,
    model: str = OPENAI_ANSWER_MODEL,
    local_validation_output: Callable[[str], None] | None = None,
    local_proposal_output: Callable[[str], None] | None = None,
) -> tuple[adapter.OpenAICitableUnitAnswerGenerator, RecordingClient]:
    client = RecordingClient(
        RecordingResponses(
            payload=payload if payload is not None else make_payload(generation_input)
        )
    )
    generator = adapter.OpenAICitableUnitAnswerGenerator(
        client=cast(OpenAI, client),
        model=model,
        local_validation_output=local_validation_output,
        local_proposal_output=local_proposal_output,
    )
    return generator, client


def test_repeated_wording_resolves_the_selected_occurrence_and_usage() -> None:
    generation_input = make_input()
    generator, _ = make_generator(generation_input)
    result = generator.generate(generation_input)
    reference = result.answer.statements[0].evidence[0]
    assert reference.start_character == SOURCE_TEXT.rfind(STATEMENT)
    assert reference.end_character == len(SOURCE_TEXT)
    assert reference.exact_text == STATEMENT
    assert reference.retrieval_candidate_id == UUID(int=7)
    assert result.usage == ProviderUsage(input_units=120, output_units=30)


def test_two_adjacent_units_remain_separate_exact_unicode_ranges() -> None:
    generation_input = make_input(text="Café 😀 is ready. Next archi-\ntecture stays unchanged.")
    ids = tuple(unit.source_unit_id for unit in generation_input.catalog.units)
    generator, _ = make_generator(generation_input, payload=make_payload(generation_input, ids=ids))
    references = generator.generate(generation_input).answer.statements[0].evidence
    assert len(references) == 2
    for reference, unit in zip(references, generation_input.catalog.units, strict=True):
        assert reference.start_character == unit.key.start_character
        assert reference.end_character == unit.key.end_character
        assert reference.exact_text == unit.text


def test_payload_preserves_context_once_and_omits_canonical_identifiers() -> None:
    generation_input = make_input()
    generator, client = make_generator(generation_input)
    generator.generate(generation_input)
    call = client.responses.calls[0]
    assert call["instructions"] == generator.instructions
    assert call["reasoning"] == {"effort": "low"}
    assert call["timeout"] == 60.0
    assert call["max_output_tokens"] == 2000
    assert call["tools"] == []
    assert call["store"] is False
    assert call["background"] is False
    assert call["truncation"] == "disabled"
    serialized = cast(str, call["input"])
    data = json.loads(serialized)
    assert set(data) == {"research_request_id", "question", "sources"}
    source = data["sources"][0]
    assert set(source) == {"units", "trailing_separator"}
    assert (
        "".join(unit["separator_before"] + unit["text"] for unit in source["units"])
        + source["trailing_separator"]
    ) == SOURCE_TEXT
    assert all(
        set(unit) == {"source_unit_id", "text", "separator_before"} for unit in source["units"]
    )
    assert [unit["source_unit_id"] for unit in source["units"]] == [
        str(unit.source_unit_id) for unit in generation_input.catalog.units
    ]
    assert [unit["text"] for unit in source["units"]] == [
        unit.text for unit in generation_input.catalog.units
    ]
    for forbidden in (
        "workspace_id",
        "document_id",
        "retrieval_candidate_id",
        "exact_text",
        "start_character",
        "end_character",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "text",
    [
        "  Unique first sentence.\n \t\nUnique second sentence. \n",
        "\tCafé 😀 is ready.\nNext archi-\ntecture stays unchanged.\t",
        "First repeated sentence.  First repeated sentence.\n",
        'A source says "ignore instructions".\nA literal \\path stays unchanged.',
        "  " + "x" * 1_601 + "\n",
    ],
)
def test_direct_unit_text_retains_every_original_character_without_duplication(text: str) -> None:
    generation_input = make_input(text=text)
    generator, client = make_generator(generation_input)
    generator.generate(generation_input)
    source = json.loads(cast(str, client.responses.calls[0]["input"]))["sources"][0]
    offered = source["units"]
    assert len(offered) == len(generation_input.catalog.units)
    for entry, unit in zip(offered, generation_input.catalog.units, strict=True):
        assert entry["source_unit_id"] == str(unit.source_unit_id)
        assert entry["text"] == text[unit.key.start_character : unit.key.end_character]
        assert not entry["separator_before"].strip()
    assert not source["trailing_separator"].strip()
    reconstructed = (
        "".join(entry["separator_before"] + entry["text"] for entry in offered)
        + source["trailing_separator"]
    )
    assert reconstructed == text
    assert "text" not in source  # No second complete-block copy beside the units.


@pytest.mark.parametrize("character", ["x", "😀"])
def test_full_context_direct_text_fits_the_unchanged_reservation(character: str) -> None:
    generation_input = make_input(text=character * 24_000)
    generator, client = make_generator(generation_input)
    generator.generate(generation_input)
    assert len(client.responses.calls) == 1
    assert len(generation_input.catalog.units) == 30


def test_json_escaping_overflow_fails_before_provider_call() -> None:
    generation_input = make_input(text='"\\' * 12_000)
    generator, client = make_generator(generation_input)
    with pytest.raises(CitableUnitLimitError):
        generator.generate(generation_input)
    assert not client.responses.calls


def test_strict_sdk_schema_has_one_shared_catalog_enum_and_required_fields() -> None:
    generation_input = make_input(text="A. " * 256)
    schema = to_strict_json_schema(adapter.citable_answer_schema(generation_input.catalog))
    definitions = schema["$defs"]
    ids = [str(unit.source_unit_id) for unit in generation_input.catalog.units]
    assert definitions["SourceUnitId"] == {"type": "string", "enum": ids}
    assert len(ids) == 256
    assert sum(map(len, ids)) < 15_000
    assert definitions["_CitableStatement"]["properties"]["source_unit_ids"]["items"] == {
        "$ref": "#/$defs/SourceUnitId"
    }
    assert len(schema["properties"]) == 4
    assert set(schema["required"]) == set(schema["properties"])
    for value in (schema, definitions["_CitableStatement"]):
        assert value["additionalProperties"] is False
    assert json.dumps(schema).count(ids[0]) == 1
    raw_schema = adapter.citable_answer_schema(generation_input.catalog).model_json_schema()
    assert (
        len(json.dumps(schema).encode("utf-8")) <= len(json.dumps(raw_schema).encode("utf-8")) + 512
    )


@pytest.mark.parametrize("bad_ids", ["unknown", "cross_request", "duplicate", "mixed"])
def test_invalid_selected_handles_fail_independently_of_provider_enum(bad_ids: str) -> None:
    generation_input = make_input()
    valid = generation_input.catalog.units[0].source_unit_id
    foreign = make_input(request_id=UUID(int=99)).catalog.units[0].source_unit_id
    choices = {
        "unknown": (UUID(int=100),),
        "cross_request": (foreign,),
        "duplicate": (valid, valid),
        "mixed": (valid, foreign),
    }
    generator, client = make_generator(
        generation_input, payload=make_payload(generation_input, ids=choices[bad_ids])
    )
    with pytest.raises(OpenAIAnswerProviderContractError) as captured:
        generator.generate(generation_input)
    assert captured.value.detail == "invalid_source_units"
    assert len(client.responses.calls) == 1


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("exact_text", "Model copied text"),
        ("start_character", 0),
        ("retrieval_candidate_id", str(UUID(int=7))),
    ],
)
def test_model_cannot_return_quote_offsets_or_candidate_ids(field_name: str, value: object) -> None:
    generation_input = make_input()
    payload = make_payload(generation_input)
    statements = cast(list[dict[str, object]], payload["statements"])
    statements[0][field_name] = value
    generator, _ = make_generator(generation_input, payload=payload)
    with pytest.raises(OpenAIAnswerProviderContractError) as captured:
        generator.generate(generation_input)
    assert captured.value.detail == "provider_schema_validation"


@pytest.mark.parametrize("count", [0, 3])
def test_model_must_select_one_or_two_units(count: int) -> None:
    generation_input = make_input(text="A. B. C.")
    ids = tuple(unit.source_unit_id for unit in generation_input.catalog.units)[:count]
    generator, _ = make_generator(generation_input, payload=make_payload(generation_input, ids=ids))
    with pytest.raises(OpenAIAnswerProviderContractError):
        generator.generate(generation_input)


@pytest.mark.parametrize("coverage", list(CoverageStatus))
def test_four_coverage_states_retain_existing_business_rules(coverage: CoverageStatus) -> None:
    generation_input = make_input()
    missing = "No source supports an answer." if coverage is CoverageStatus.INSUFFICIENT else None
    generator, _ = make_generator(
        generation_input, payload=make_payload(generation_input, coverage=coverage, missing=missing)
    )
    answer = generator.generate(generation_input).answer
    assert answer.coverage_status is coverage
    assert bool(answer.statements) is (coverage is not CoverageStatus.INSUFFICIENT)
    assert answer.missing_information == missing


def test_partial_gap_text_is_not_promoted_into_the_existing_answer_contract() -> None:
    generation_input = make_input()
    generator, _ = make_generator(
        generation_input,
        payload=make_payload(
            generation_input, coverage=CoverageStatus.PARTIAL, missing="An unverified model gap."
        ),
    )
    assert generator.generate(generation_input).answer.missing_information is None


@pytest.mark.parametrize(
    "case", ["empty", "wrong_input", "request", "workspace", "sources", "overhead"]
)
def test_invalid_or_empty_input_never_calls_a_provider(case: str) -> None:
    generation_input = make_input(
        empty=case == "empty", text="A. " * 256 if case == "overhead" else SOURCE_TEXT
    )
    generator, client = make_generator(generation_input, payload={})
    selected: AnswerGenerationInput = generation_input
    if case == "wrong_input":
        selected = AnswerGenerationInput(
            research_request_id=REQUEST_ID,
            question=generation_input.question,
            sources=generation_input.sources,
        )
    elif case == "request":
        selected = generation_input.model_copy(update={"research_request_id": UUID(int=90)})
    elif case == "workspace":
        selected = generation_input.model_copy(
            update={"context": WorkspaceContext(user_id=CONTEXT.user_id, workspace_id=UUID(int=90))}
        )
    elif case == "sources":
        selected = generation_input.model_copy(update={"sources": ()})
    with pytest.raises(CitableUnitLimitError if case == "overhead" else CitableUnitError):
        generator.generate(selected)
    assert client.responses.calls == []


def test_input_constructor_checks_catalog_scope_and_retains_generation_bounds() -> None:
    generation_input = make_input()
    for updates in ({"research_request_id": UUID(int=99)}, {"sources": ()}, {"question": " "}):
        with pytest.raises(ValidationError):
            CitableAnswerGenerationInput.model_validate(generation_input.model_dump() | updates)


@pytest.mark.parametrize(
    "bad_usage",
    [
        None,
        SimpleNamespace(input_tokens=True, output_tokens=30, total_tokens=31),
        SimpleNamespace(input_tokens=-1, output_tokens=30, total_tokens=29),
        SimpleNamespace(input_tokens=120, output_tokens=30, total_tokens=149),
    ],
)
def test_missing_or_invalid_usage_fails_closed(bad_usage: object) -> None:
    generation_input = make_input()
    generator, client = make_generator(generation_input)
    client.responses.overrides["usage"] = bad_usage
    with pytest.raises(OpenAIAnswerProviderContractError) as captured:
        generator.generate(generation_input)
    assert captured.value.detail == ("missing_usage" if bad_usage is None else "invalid_usage")


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_safe_provider_status_classification_and_no_retries(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    generation_input = make_input()
    generator, client = make_generator(generation_input)
    client.responses.error = APIStatusError(
        "SENSITIVE_PROVIDER_MESSAGE",
        response=Response(status, request=Request("POST", "https://api.openai.com/v1/responses")),
        body=None,
    )
    expected = (
        OpenAIAnswerProviderConfigurationError
        if status < 429
        else AnswerGenerationProviderTransientError
    )
    with caplog.at_level(logging.WARNING), pytest.raises(expected) as captured:
        generator.generate(generation_input)
    assert "SENSITIVE_PROVIDER_MESSAGE" not in str(captured.value) + caplog.text
    assert SOURCE_TEXT not in caplog.text
    assert len(client.responses.calls) == 1


def test_connection_error_is_safe_and_not_retried() -> None:
    generation_input = make_input()
    generator, client = make_generator(generation_input)
    client.responses.error = APIConnectionError(
        request=Request("POST", "https://api.openai.com/v1/responses")
    )
    with pytest.raises(AnswerGenerationProviderTransientError):
        generator.generate(generation_input)
    assert len(client.responses.calls) == 1


@pytest.mark.parametrize("case", ["model", "incomplete", "refusal", "untyped"])
def test_response_envelope_and_refusals_are_checked(case: str) -> None:
    generation_input = make_input()
    generator, client = make_generator(generation_input)
    expected: type[Exception] = OpenAIAnswerProviderContractError
    if case == "model":
        client.responses.overrides["model"] = "unknown-model"
    elif case == "incomplete":
        client.responses.overrides.update(
            status="incomplete", incomplete_details=SimpleNamespace(reason="max_output_tokens")
        )
        expected = AnswerGenerationProviderOutputLimitError
    elif case == "refusal":
        client.responses.overrides.update(
            output_parsed=None,
            output=(SimpleNamespace(type="message", content=(SimpleNamespace(type="refusal"),)),),
        )
        expected = AnswerGenerationProviderRefusalError
    else:
        client.responses.overrides["output_parsed"] = SimpleNamespace()
    with pytest.raises(expected):
        generator.generate(generation_input)


@pytest.mark.parametrize("invalid_schema", [False, True])
def test_local_failure_output_requires_explicit_callback(
    invalid_schema: bool, caplog: pytest.LogCaptureFixture
) -> None:
    generation_input = make_input()
    outputs: list[str] = []
    payload = make_payload(generation_input, ids=(UUID(int=99),))
    if invalid_schema:
        payload["unexpected"] = "UNVALIDATED_CONTENT"
    generator, client = make_generator(
        generation_input, payload=payload, local_validation_output=outputs.append
    )
    with caplog.at_level(logging.WARNING), pytest.raises(OpenAIAnswerProviderContractError):
        generator.generate(generation_input)
    assert client.responses.raw_route
    assert len(outputs) == 1
    assert json.loads(outputs[0])["statements"] == payload["statements"]
    assert "UNVALIDATED_CONTENT" not in caplog.text
    assert SOURCE_TEXT not in caplog.text


def test_opt_in_proposal_output_does_not_authorize_a_known_id_claim() -> None:
    generation_input = make_input()
    outputs: list[str] = []
    payload = make_payload(generation_input)
    statements = cast(list[dict[str, object]], payload["statements"])
    statements[0]["text"] = "A fabricated unsupported claim."
    generator, client = make_generator(
        generation_input, payload=payload, local_proposal_output=outputs.append
    )
    result = generator.generate(generation_input)
    assert client.responses.raw_route
    assert len(outputs) == 1
    assert result.answer.statements[0].text == "A fabricated unsupported claim."
    # Resolution is not a support evaluator or persistence authorization.
    assert result.answer.statements[0].evidence[0].exact_text == STATEMENT


@pytest.mark.parametrize("model", list(ResearchModel))
def test_profiles_version_the_model_and_unit_policy_separately_from_m1(
    model: ResearchModel,
) -> None:
    profile = adapter.citable_answer_profile(model)
    assert profile.generator_version != M1_ANSWER_GENERATOR_VERSION
    assert profile.prompt_version != M1_ANSWER_PROMPT_VERSION
    assert model.value in profile.generation_config_version
    assert profile.generator_version == "openai-responses-citable-units-v2"
    assert profile.prompt_version == "m1-5-citable-units-en-answer-prompt-v2"
    assert "citable-units-en-v2" in profile.generation_config_version
    assert make_input().catalog.unit_policy_version == "m1-5-citable-units-en-v1"


def test_factory_disables_sdk_retries_and_closes_its_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RecordingClient(RecordingResponses(payload={}))
    calls: list[dict[str, object]] = []

    def create_client(**kwargs: object) -> OpenAI:
        calls.append(kwargs)
        return cast(OpenAI, client)

    monkeypatch.setattr(adapter, "OpenAI", create_client)
    generator = adapter.create_openai_citable_unit_answer_generator(api_key="synthetic-test-key")
    assert calls == [{"api_key": "synthetic-test-key", "timeout": 60.0, "max_retries": 0}]
    generator.close()
    assert client.closed
    with pytest.raises(ValueError):
        adapter.create_openai_citable_unit_answer_generator(api_key=" ")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "case",
    [
        "blank_statement",
        "answered_without_statements",
        "complete_gap",
        "insufficient_statements",
        "insufficient_without_gap",
    ],
)
def test_invalid_answer_business_shapes_remain_provider_contract_failures(case: str) -> None:
    generation_input = make_input()
    payload = make_payload(generation_input)
    if case == "blank_statement":
        statements = cast(list[dict[str, object]], payload["statements"])
        statements[0]["text"] = " "
    elif case == "answered_without_statements":
        payload["statements"] = []
    elif case == "complete_gap":
        payload["missing_information"] = "Unverified gap"
    elif case == "insufficient_statements":
        payload["coverage_status"] = "INSUFFICIENT"
        payload["missing_information"] = "No evidence"
    else:
        payload["coverage_status"] = "INSUFFICIENT"
        payload["statements"] = []
    generator, _ = make_generator(generation_input, payload=payload)
    with pytest.raises(OpenAIAnswerProviderContractError) as captured:
        generator.generate(generation_input)
    assert captured.value.detail == "proposal_validation"


@pytest.mark.parametrize("model", list(ResearchModel))
def test_both_approved_models_use_the_same_id_contract(model: ResearchModel) -> None:
    generation_input = make_input()
    generator, client = make_generator(generation_input, model=model.value)
    client.responses.overrides["model"] = model.value
    assert generator.generate(generation_input).answer.coverage_status is CoverageStatus.COMPLETE
    assert client.responses.calls[0]["model"] == model.value


def test_metadata_reservation_boundary_is_enforced_before_a_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation_input = make_input()
    generator, client = make_generator(generation_input)
    schema = adapter.citable_answer_schema(generation_input.catalog)
    maximum_input = (
        len(generator.instructions.encode("utf-8"))
        + len(generator._serialize_input(generation_input).encode("utf-8"))
        + len(json.dumps(schema.model_json_schema()).encode("utf-8"))
        + 1024
    )
    content = len(generation_input.question.encode("utf-8")) + sum(
        len(source.text.encode("utf-8")) for source in generation_input.sources
    )
    overhead = maximum_input - content
    # SDK reference expansion fits the dedicated margin; request framing keeps its own margin.
    assert len(json.dumps(to_strict_json_schema(schema)).encode("utf-8")) <= (
        len(json.dumps(schema.model_json_schema()).encode("utf-8")) + 512
    )
    monkeypatch.setattr(adapter, "OPENAI_ANSWER_ACCOUNTING_INPUT_OVERHEAD_UNITS", overhead)
    generator.generate(generation_input)
    assert len(client.responses.calls) == 1
    monkeypatch.setattr(adapter, "OPENAI_ANSWER_ACCOUNTING_INPUT_OVERHEAD_UNITS", overhead - 1)
    with pytest.raises(CitableUnitLimitError):
        generator.generate(generation_input)
    assert len(client.responses.calls) == 1


def test_tampered_catalog_is_revalidated_before_calling_provider() -> None:
    generation_input = make_input()
    unit = generation_input.catalog.units[0].model_copy(update={"source_unit_id": UUID(int=999)})
    catalog = generation_input.catalog.model_copy(
        update={"units": (unit, *generation_input.catalog.units[1:])}
    )
    invalid = generation_input.model_copy(update={"catalog": catalog})
    generator, client = make_generator(generation_input)
    with pytest.raises(CitableUnitError):
        generator.generate(invalid)
    assert not client.responses.calls


def test_each_request_gets_a_fresh_schema_without_shared_catalog_state() -> None:
    first = make_input()
    second = make_input(request_id=UUID(int=500))
    generator, client = make_generator(first)
    generator.generate(first)
    client.responses.payload = make_payload(second)
    generator.generate(second)
    schemas = [
        cast(type[BaseModel], call["text_format"]).model_json_schema()
        for call in client.responses.calls
    ]
    first_ids = schemas[0]["$defs"]["SourceUnitId"]["enum"]
    second_ids = schemas[1]["$defs"]["SourceUnitId"]["enum"]
    assert first_ids == [str(unit.source_unit_id) for unit in first.catalog.units]
    assert second_ids == [str(unit.source_unit_id) for unit in second.catalog.units]
    assert set(first_ids).isdisjoint(second_ids)


def test_two_different_source_units_keep_their_own_provenance_and_context() -> None:
    first = make_input().catalog.sources[0]
    text = "The PDF confirms the pilot will proceed."
    block = first.block.model_copy(
        update={
            "id": UUID(int=104),
            "processing_run_id": UUID(int=105),
            "document_version_id": UUID(int=106),
            "source_locator_kind": SourceLocatorKind.PDF_PAGE,
            "page_number": 4,
            "text": text,
            "end_character": 40 + len(text),
        }
    )
    second = CitableUnitSource(
        block=block,
        source=first.source.model_copy(
            update={
                "retrieval_candidate_id": UUID(int=107),
                "chunk_id": UUID(int=108),
                "document_id": UUID(int=109),
                "document_version_id": block.document_version_id,
                "structure_block_id": block.id,
                "source_locator_kind": SourceLocatorKind.PDF_PAGE,
                "page_number": 4,
                "text": text,
            }
        ),
    )
    catalog = build_citable_unit_catalog(
        context=CONTEXT, research_request_id=REQUEST_ID, sources=(first, second)
    )
    generation_input = CitableAnswerGenerationInput.from_catalog(
        context=CONTEXT, question="What does each source say?", catalog=catalog
    )
    ids = (catalog.units[0].source_unit_id, catalog.units[-1].source_unit_id)
    generator, client = make_generator(
        generation_input, payload=make_payload(generation_input, ids=ids)
    )
    references = generator.generate(generation_input).answer.statements[0].evidence
    assert tuple(reference.retrieval_candidate_id for reference in references) == (
        UUID(int=7),
        UUID(int=107),
    )
    assert tuple(reference.exact_text for reference in references) == (STATEMENT, text)
    data = json.loads(cast(str, client.responses.calls[0]["input"]))
    assert [
        "".join(unit["separator_before"] + unit["text"] for unit in source["units"])
        + source["trailing_separator"]
        for source in data["sources"]
    ] == [SOURCE_TEXT, text]
    assert [len(source["units"]) for source in data["sources"]] == [2, 1]
    assert [unit["source_unit_id"] for source in data["sources"] for unit in source["units"]] == [
        str(unit.source_unit_id) for unit in catalog.units
    ]
