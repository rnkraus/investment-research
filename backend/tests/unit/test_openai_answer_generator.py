"""Unit tests for the bounded OpenAI answer generator."""

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from httpx2 import Request, Response
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    LengthFinishReasonError,
    OpenAI,
    RateLimitError,
)
from openai.types.chat import ChatCompletion
from pydantic import ValidationError

from investment_research.infrastructure.generation import openai as openai_module
from investment_research.infrastructure.generation.openai import (
    M1_ANSWER_PROMPT_VERSION,
    OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
    OPENAI_ANSWER_MODEL,
    OPENAI_ANSWER_TIMEOUT_SECONDS,
    OpenAIAnswerGenerator,
    OpenAIAnswerProviderConfigurationError,
    OpenAIAnswerProviderContractError,
    create_openai_answer_generator,
)
from investment_research.infrastructure.logging import SafeJsonFormatter
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationSource,
    UnvalidatedEvidenceReference,
    UnvalidatedGeneratedAnswer,
    UnvalidatedGeneratedStatement,
)
from investment_research.modules.research.domain import CoverageStatus
from investment_research.modules.research.ports import (
    AnswerGenerationProviderConfigurationError,
    AnswerGenerationProviderContractError,
    AnswerGenerationProviderOutputLimitError,
    AnswerGenerationProviderRefusalError,
    AnswerGenerationProviderTransientError,
)

REQUEST_ID = UUID("10000000-0000-4000-8000-000000000001")
CANDIDATE_ID = UUID("10000000-0000-4000-8000-000000000002")
CHUNK_ID = UUID("10000000-0000-4000-8000-000000000003")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000004")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000005")
BLOCK_ID = UUID("10000000-0000-4000-8000-000000000006")
QUESTION = "What constrained new customer deployments?"
SOURCE_TEXT = "A shortage of high-voltage transformers constrained new customer deployments."
STATEMENT = "A transformer shortage constrained new customer deployments."


def make_generation_input(*, source_text: str = SOURCE_TEXT) -> AnswerGenerationInput:
    """Build one bounded canonical source passed to the untrusted generator."""

    return AnswerGenerationInput(
        research_request_id=REQUEST_ID,
        question=QUESTION,
        sources=(
            AnswerGenerationSource(
                retrieval_candidate_id=CANDIDATE_ID,
                chunk_id=CHUNK_ID,
                document_id=DOCUMENT_ID,
                document_version_id=VERSION_ID,
                structure_block_id=BLOCK_ID,
                source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
                text=source_text,
            ),
        ),
    )


def make_proposal() -> UnvalidatedGeneratedAnswer:
    """Build one schema-valid but still untrusted answer proposal."""

    return UnvalidatedGeneratedAnswer(
        coverage_status=CoverageStatus.COMPLETE,
        rendered_text=STATEMENT,
        statements=(
            UnvalidatedGeneratedStatement(
                text=STATEMENT,
                evidence=(
                    UnvalidatedEvidenceReference(
                        retrieval_candidate_id=CANDIDATE_ID,
                        start_character=0,
                        end_character=len(SOURCE_TEXT),
                        exact_text=SOURCE_TEXT,
                    ),
                ),
            ),
        ),
    )


def make_provider_proposal(
    *,
    exact_text: str = SOURCE_TEXT,
    candidate_id: UUID = CANDIDATE_ID,
) -> openai_module._OpenAIGeneratedAnswer:
    """Build the provider shape without model-authored character positions."""

    return openai_module._OpenAIGeneratedAnswer(
        coverage_status=CoverageStatus.COMPLETE,
        rendered_text=STATEMENT,
        statements=(
            openai_module._OpenAIGeneratedStatement(
                text=STATEMENT,
                evidence=(
                    openai_module._OpenAIEvidenceReference(
                        retrieval_candidate_id=candidate_id,
                        exact_text=exact_text,
                    ),
                ),
            ),
        ),
    )


@dataclass(slots=True)
class StubResponseUsage:
    """Expose the bounded token totals consumed from an OpenAI response."""

    input_tokens: int = 120
    output_tokens: int = 30
    total_tokens: int = 150


@dataclass(slots=True)
class StubParsedResponse:
    """Expose the parsed response fields consumed by the adapter."""

    model: str = OPENAI_ANSWER_MODEL
    status: str = "completed"
    output_parsed: openai_module._OpenAIGeneratedAnswer | None = field(
        default_factory=make_provider_proposal
    )
    output: tuple[object, ...] = ()
    incomplete_details: object | None = None
    usage: StubResponseUsage | None = field(default_factory=StubResponseUsage)


@dataclass(slots=True)
class RecordingResponsesResource:
    """Record one Responses API call or raise a configured failure."""

    response: StubParsedResponse = field(default_factory=StubParsedResponse)
    error: BaseException | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def parse(self, **kwargs: object) -> StubParsedResponse:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


@dataclass(slots=True)
class RecordingOpenAIClient:
    """Expose only the SDK surface used by the adapter and record shutdown."""

    responses: RecordingResponsesResource = field(default_factory=RecordingResponsesResource)
    closed: bool = False

    def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class StubRawResponse:
    """Expose the raw provider body before the pinned SDK parser validates it."""

    body: object
    parse_error: ValidationError

    @property
    def http_response(self) -> StubRawResponse:
        return self

    def parse(self) -> StubParsedResponse:
        raise self.parse_error

    def json(self) -> object:
        return self.body


@dataclass(slots=True)
class RecordingRawResponsesResource:
    """Require the raw-response route while recording the single provider request."""

    raw_response: StubRawResponse
    calls: list[dict[str, object]] = field(default_factory=list)
    raw_route_used: bool = False

    @property
    def with_raw_response(self) -> RecordingRawResponsesResource:
        self.raw_route_used = True
        return self

    def parse(self, **kwargs: object) -> StubRawResponse:
        self.calls.append(kwargs)
        return self.raw_response


@dataclass(slots=True)
class RecordingRawOpenAIClient:
    responses: RecordingRawResponsesResource

    def close(self) -> None:
        pass


def make_generator(client: RecordingOpenAIClient) -> OpenAIAnswerGenerator:
    """Inject a recording client through the concrete SDK boundary."""

    return OpenAIAnswerGenerator(client=cast(OpenAI, client))


def make_status_response(status_code: int) -> Response:
    """Build the HTTP response required by concrete SDK status errors."""

    return Response(
        status_code,
        request=Request("POST", "https://api.openai.com/v1/responses"),
    )


def test_generate_sends_one_bounded_stateless_request_with_allowlisted_data() -> None:
    client = RecordingOpenAIClient()

    result = make_generator(client).generate(make_generation_input())

    assert result.answer == make_proposal()
    assert result.usage == ProviderUsage(input_units=120, output_units=30)
    assert len(client.responses.calls) == 1
    call = client.responses.calls[0]
    assert call["model"] == OPENAI_ANSWER_MODEL
    assert call["text_format"] is openai_module._OpenAIGeneratedAnswer
    assert call["reasoning"] == {"effort": "low"}
    assert call["max_output_tokens"] == OPENAI_ANSWER_MAX_OUTPUT_TOKENS
    assert call["store"] is False
    assert call["background"] is False
    assert call["tools"] == []
    assert call["truncation"] == "disabled"
    assert call["timeout"] == OPENAI_ANSWER_TIMEOUT_SECONDS
    assert "untrusted data" in cast(str, call["instructions"])
    payload = json.loads(cast(str, call["input"]))
    assert payload == {
        "question": QUESTION,
        "research_request_id": str(REQUEST_ID),
        "sources": [
            {
                "retrieval_candidate_id": str(CANDIDATE_ID),
                "text": SOURCE_TEXT,
            }
        ],
    }
    serialized_payload = cast(str, call["input"])
    assert str(CHUNK_ID) not in serialized_payload
    assert str(DOCUMENT_ID) not in serialized_payload
    assert str(VERSION_ID) not in serialized_payload
    assert str(BLOCK_ID) not in serialized_payload


def test_luna_receives_versioned_verbatim_evidence_instructions() -> None:
    client = RecordingOpenAIClient(
        responses=RecordingResponsesResource(response=StubParsedResponse(model="gpt-5.6-luna"))
    )
    generator = OpenAIAnswerGenerator(client=cast(OpenAI, client), model="gpt-5.6-luna")

    generator.generate(make_generation_input())

    instructions = cast(str, client.responses.calls[0]["instructions"])
    assert M1_ANSWER_PROMPT_VERSION == "m1-grounded-answer-prompt-v3"
    assert "exact_text character-for-character" in instructions
    assert "Do not paraphrase, translate, normalize, correct" in instructions
    assert "each exact_text occurs exactly once in its cited source" in instructions
    assert (
        "If no unique verbatim substring supports a statement, omit that statement" in instructions
    )


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        (
            StubParsedResponse(
                model="gpt-5.6-luna",
                status="incomplete",
                output_parsed=None,
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            ),
            "output_limit",
        ),
        (
            StubParsedResponse(
                model="gpt-5.6-luna",
                output_parsed=make_provider_proposal(candidate_id=DOCUMENT_ID),
            ),
            "unknown_candidate",
        ),
    ],
)
def test_luna_generation_failure_logs_only_safe_diagnostic(
    response: StubParsedResponse,
    expected_reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = RecordingOpenAIClient(responses=RecordingResponsesResource(response=response))
    generator = OpenAIAnswerGenerator(client=cast(OpenAI, client), model="gpt-5.6-luna")

    with pytest.raises(AnswerGenerationProviderContractError):
        generator.generate(make_generation_input())

    records = [
        record
        for record in caplog.records
        if record.name == "investment_research.infrastructure.generation.openai"
    ]
    assert len(records) == 1
    expected_event = f"answer_generation.provider_failed.gpt-5.6-luna.{expected_reason}"
    assert records[0].message == expected_event
    assert expected_event in SafeJsonFormatter(process_name="api").format(records[0])
    assert QUESTION not in caplog.text
    assert SOURCE_TEXT not in caplog.text


@pytest.mark.parametrize(
    ("source_text", "exact_text", "expected_start"),
    [
        pytest.param(
            "The primary constraint on new customer deployments is "
            "a shortage of high-voltage transformers.",
            "a shortage of high-voltage transformers",
            54,
            id="punctuation-is-not-part-of-evidence",
        ),
        pytest.param(
            "Before 🧭 Evidence after",
            "🧭 Evidence",
            7,
            id="unicode-code-point-offsets",
        ),
    ],
)
def test_generate_derives_exact_unique_source_offsets(
    source_text: str,
    exact_text: str,
    expected_start: int,
) -> None:
    client = RecordingOpenAIClient(
        responses=RecordingResponsesResource(
            response=StubParsedResponse(
                output_parsed=make_provider_proposal(exact_text=exact_text),
            )
        )
    )

    result = make_generator(client).generate(make_generation_input(source_text=source_text))

    reference = result.answer.statements[0].evidence[0]
    assert reference.start_character == expected_start
    assert reference.end_character == expected_start + len(exact_text)
    assert source_text[reference.start_character : reference.end_character] == exact_text
    assert len(client.responses.calls) == 1
    assert set(openai_module._OpenAIEvidenceReference.model_fields) == {
        "retrieval_candidate_id",
        "exact_text",
    }


@pytest.mark.parametrize(
    ("source_text", "exact_text", "candidate_id", "expected_detail"),
    [
        pytest.param(
            "No matching passage",
            "different passage",
            CANDIDATE_ID,
            "quote_not_found",
            id="missing-text",
        ),
        pytest.param(
            "repeated text; repeated text",
            "repeated text",
            CANDIDATE_ID,
            "quote_ambiguous",
            id="ambiguous-text",
        ),
        pytest.param(
            SOURCE_TEXT,
            SOURCE_TEXT,
            REQUEST_ID,
            "unknown_candidate",
            id="unknown-candidate",
        ),
    ],
)
def test_generate_rejects_unresolvable_evidence_without_guessing(
    source_text: str,
    exact_text: str,
    candidate_id: UUID,
    expected_detail: str,
) -> None:
    client = RecordingOpenAIClient(
        responses=RecordingResponsesResource(
            response=StubParsedResponse(
                output_parsed=make_provider_proposal(
                    exact_text=exact_text,
                    candidate_id=candidate_id,
                ),
            )
        )
    )

    with pytest.raises(OpenAIAnswerProviderContractError) as captured:
        make_generator(client).generate(make_generation_input(source_text=source_text))

    assert captured.value.detail == expected_detail
    assert captured.value.__cause__ is None
    assert len(client.responses.calls) == 1


@pytest.mark.parametrize(
    ("response", "application_error", "expected_detail"),
    [
        pytest.param(
            StubParsedResponse(model="different-model"),
            AnswerGenerationProviderContractError,
            "model_mismatch",
            id="model-drift",
        ),
        pytest.param(
            StubParsedResponse(status="incomplete", output_parsed=None),
            AnswerGenerationProviderContractError,
            "incomplete_response",
            id="incomplete-without-output-limit",
        ),
        pytest.param(
            StubParsedResponse(
                status="incomplete",
                output_parsed=None,
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            ),
            AnswerGenerationProviderOutputLimitError,
            None,
            id="output-limit",
        ),
        pytest.param(
            StubParsedResponse(status="failed", output_parsed=None),
            AnswerGenerationProviderTransientError,
            None,
            id="failed",
        ),
        pytest.param(
            StubParsedResponse(output_parsed=None),
            AnswerGenerationProviderContractError,
            "missing_typed_answer",
            id="missing-typed-output",
        ),
        pytest.param(
            StubParsedResponse(
                output_parsed=None,
                output=(
                    SimpleNamespace(
                        type="message",
                        content=(SimpleNamespace(type="refusal"),),
                    ),
                ),
            ),
            AnswerGenerationProviderRefusalError,
            None,
            id="refusal",
        ),
        pytest.param(
            StubParsedResponse(usage=None),
            AnswerGenerationProviderContractError,
            "missing_usage",
            id="missing-usage",
        ),
        pytest.param(
            StubParsedResponse(usage=StubResponseUsage(total_tokens=151)),
            AnswerGenerationProviderContractError,
            "invalid_usage",
            id="contradictory-usage",
        ),
        pytest.param(
            StubParsedResponse(
                usage=StubResponseUsage(
                    input_tokens=9_223_372_036_854_775_808,
                    output_tokens=0,
                    total_tokens=9_223_372_036_854_775_808,
                )
            ),
            AnswerGenerationProviderContractError,
            "invalid_usage",
            id="usage-out-of-range",
        ),
    ],
)
def test_generate_rejects_non_completed_or_untyped_responses(
    response: StubParsedResponse,
    application_error: type[Exception],
    expected_detail: str | None,
) -> None:
    client = RecordingOpenAIClient(responses=RecordingResponsesResource(response=response))

    with pytest.raises(application_error) as captured:
        make_generator(client).generate(make_generation_input())

    if expected_detail is not None:
        assert isinstance(captured.value, OpenAIAnswerProviderContractError)
        assert captured.value.detail == expected_detail


@pytest.mark.parametrize(
    ("provider_error", "application_error"),
    [
        pytest.param(
            APIConnectionError(
                message="sensitive transport detail",
                request=Request("POST", "https://api.openai.com/v1/responses"),
            ),
            AnswerGenerationProviderTransientError,
            id="connection",
        ),
        pytest.param(
            RateLimitError(
                "sensitive rate-limit detail",
                response=make_status_response(429),
                body=None,
            ),
            AnswerGenerationProviderTransientError,
            id="rate-limit",
        ),
        pytest.param(
            InternalServerError(
                "sensitive service detail",
                response=make_status_response(500),
                body=None,
            ),
            AnswerGenerationProviderTransientError,
            id="service",
        ),
        pytest.param(
            AuthenticationError(
                "sensitive authentication detail",
                response=make_status_response(401),
                body=None,
            ),
            AnswerGenerationProviderConfigurationError,
            id="authentication",
        ),
        pytest.param(
            APIResponseValidationError(
                response=make_status_response(200),
                body={"sensitive": "provider detail"},
                message="sensitive schema detail",
            ),
            AnswerGenerationProviderContractError,
            id="response-schema",
        ),
        pytest.param(
            LengthFinishReasonError(completion=cast(ChatCompletion, SimpleNamespace(usage=None))),
            AnswerGenerationProviderOutputLimitError,
            id="length-finish-reason",
        ),
    ],
)
def test_provider_errors_are_translated_without_exposing_details(
    provider_error: Exception,
    application_error: type[Exception],
) -> None:
    client = RecordingOpenAIClient(responses=RecordingResponsesResource(error=provider_error))

    with pytest.raises(application_error) as captured:
        make_generator(client).generate(make_generation_input())

    assert "sensitive" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert len(client.responses.calls) == 1
    if isinstance(provider_error, APIResponseValidationError):
        assert isinstance(captured.value, OpenAIAnswerProviderContractError)
        assert captured.value.detail == "api_response_validation"


def test_provider_schema_validation_failure_retains_only_a_fixed_detail() -> None:
    with pytest.raises(ValidationError) as invalid_output:
        openai_module._OpenAIGeneratedAnswer.model_validate(
            {"coverage_status": CoverageStatus.COMPLETE}
        )
    client = RecordingOpenAIClient(responses=RecordingResponsesResource(error=invalid_output.value))

    with pytest.raises(OpenAIAnswerProviderContractError) as captured:
        make_generator(client).generate(make_generation_input())

    assert captured.value.detail == "provider_schema_validation"
    assert "coverage_status" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_local_debug_reads_only_raw_generated_text_from_one_request() -> None:
    with pytest.raises(ValidationError) as invalid_output:
        openai_module._OpenAIGeneratedAnswer.model_validate(
            {"coverage_status": CoverageStatus.COMPLETE}
        )
    raw_response = StubRawResponse(
        body={
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": '{"rendered_text":"synthetic answer"}'},
                        {"type": "refusal", "refusal": "private provider detail"},
                    ],
                }
            ],
            "metadata": {"secret": "private provider metadata"},
        },
        parse_error=invalid_output.value,
    )
    raw_responses = RecordingRawResponsesResource(raw_response=raw_response)
    raw_client = RecordingRawOpenAIClient(responses=raw_responses)
    output: list[str] = []

    with pytest.raises(OpenAIAnswerProviderContractError) as captured:
        OpenAIAnswerGenerator(
            client=cast(OpenAI, raw_client),
            local_validation_output=output.append,
        ).generate(make_generation_input())

    normal_responses = RecordingResponsesResource()
    make_generator(RecordingOpenAIClient(responses=normal_responses)).generate(
        make_generation_input()
    )
    assert captured.value.detail == "provider_schema_validation"
    assert output == ['{"rendered_text":"synthetic answer"}']
    assert raw_responses.raw_route_used is True
    assert raw_responses.calls == normal_responses.calls
    assert len(raw_responses.calls) == 1


@pytest.mark.parametrize(
    ("provider_error", "expected_status"),
    [
        (
            BadRequestError(
                "sensitive request detail", response=make_status_response(400), body=None
            ),
            400,
        ),
        (
            AuthenticationError(
                "sensitive authentication detail",
                response=make_status_response(401),
                body=None,
            ),
            401,
        ),
    ],
)
def test_rejected_request_retains_only_safe_http_status(
    provider_error: Exception,
    expected_status: int,
) -> None:
    client = RecordingOpenAIClient(responses=RecordingResponsesResource(error=provider_error))

    with pytest.raises(OpenAIAnswerProviderConfigurationError) as captured:
        make_generator(client).generate(make_generation_input())

    assert captured.value.http_status == expected_status
    assert "sensitive" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_application_abort_is_not_translated_or_retried() -> None:
    abort = SoftTimeLimitExceeded("synthetic worker deadline")
    client = RecordingOpenAIClient(responses=RecordingResponsesResource(error=abort))

    with pytest.raises(SoftTimeLimitExceeded) as captured:
        make_generator(client).generate(make_generation_input())

    assert captured.value is abort
    assert len(client.responses.calls) == 1


@pytest.mark.parametrize(
    ("model", "operation_timeout_seconds", "max_output_tokens"),
    [
        pytest.param(
            "different-model",
            OPENAI_ANSWER_TIMEOUT_SECONDS,
            OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
            id="model",
        ),
        pytest.param(
            OPENAI_ANSWER_MODEL,
            0.0,
            OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
            id="timeout",
        ),
        pytest.param(
            OPENAI_ANSWER_MODEL,
            OPENAI_ANSWER_TIMEOUT_SECONDS,
            OPENAI_ANSWER_MAX_OUTPUT_TOKENS + 1,
            id="output-tokens",
        ),
    ],
)
def test_generator_rejects_configuration_outside_the_m1_profile(
    model: str,
    operation_timeout_seconds: float,
    max_output_tokens: int,
) -> None:
    with pytest.raises(ValueError):
        OpenAIAnswerGenerator(
            client=cast(OpenAI, RecordingOpenAIClient()),
            model=model,
            operation_timeout_seconds=operation_timeout_seconds,
            max_output_tokens=max_output_tokens,
        )


def test_factory_disables_sdk_retries_and_close_releases_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    client = RecordingOpenAIClient()

    def recording_constructor(**kwargs: object) -> RecordingOpenAIClient:
        captured.update(kwargs)
        return client

    monkeypatch.setattr(openai_module, "OpenAI", recording_constructor)

    generator = create_openai_answer_generator(api_key="synthetic-test-key")
    generator.close()

    assert captured == {
        "api_key": "synthetic-test-key",
        "timeout": OPENAI_ANSWER_TIMEOUT_SECONDS,
        "max_retries": 0,
    }
    assert client.closed is True
