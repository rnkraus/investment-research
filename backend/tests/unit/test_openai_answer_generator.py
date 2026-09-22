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
    InternalServerError,
    LengthFinishReasonError,
    OpenAI,
    RateLimitError,
)
from openai.types.chat import ChatCompletion

from investment_research.infrastructure.generation import openai as openai_module
from investment_research.infrastructure.generation.openai import (
    OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
    OPENAI_ANSWER_MODEL,
    OPENAI_ANSWER_TIMEOUT_SECONDS,
    OpenAIAnswerGenerator,
    create_openai_answer_generator,
)
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


def make_generation_input() -> AnswerGenerationInput:
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
                text=SOURCE_TEXT,
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
    output_parsed: UnvalidatedGeneratedAnswer | None = field(default_factory=make_proposal)
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
    assert call["text_format"] is UnvalidatedGeneratedAnswer
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


@pytest.mark.parametrize(
    ("response", "application_error"),
    [
        pytest.param(
            StubParsedResponse(model="different-model"),
            AnswerGenerationProviderContractError,
            id="model-drift",
        ),
        pytest.param(
            StubParsedResponse(status="incomplete", output_parsed=None),
            AnswerGenerationProviderContractError,
            id="incomplete-without-output-limit",
        ),
        pytest.param(
            StubParsedResponse(
                status="incomplete",
                output_parsed=None,
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            ),
            AnswerGenerationProviderOutputLimitError,
            id="output-limit",
        ),
        pytest.param(
            StubParsedResponse(status="failed", output_parsed=None),
            AnswerGenerationProviderTransientError,
            id="failed",
        ),
        pytest.param(
            StubParsedResponse(output_parsed=None),
            AnswerGenerationProviderContractError,
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
            id="refusal",
        ),
        pytest.param(
            StubParsedResponse(usage=None),
            AnswerGenerationProviderContractError,
            id="missing-usage",
        ),
        pytest.param(
            StubParsedResponse(usage=StubResponseUsage(total_tokens=151)),
            AnswerGenerationProviderContractError,
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
            id="usage-out-of-range",
        ),
    ],
)
def test_generate_rejects_non_completed_or_untyped_responses(
    response: StubParsedResponse,
    application_error: type[Exception],
) -> None:
    client = RecordingOpenAIClient(responses=RecordingResponsesResource(response=response))

    with pytest.raises(application_error):
        make_generator(client).generate(make_generation_input())


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
