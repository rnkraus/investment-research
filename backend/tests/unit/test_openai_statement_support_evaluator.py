"""Unit tests for the bounded OpenAI statement-support evaluator."""

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast

import pytest
from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from httpx2 import Request, Response
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    AuthenticationError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from pydantic import ValidationError

from investment_research.infrastructure.evidence import openai as openai_module
from investment_research.infrastructure.evidence.openai import (
    OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
    OPENAI_STATEMENT_SUPPORT_MODEL,
    OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS,
    OpenAIStatementSupportEvaluator,
    create_openai_statement_support_evaluator,
)
from investment_research.modules.evidence.contracts import StatementSupportDecision
from investment_research.modules.evidence.ports import (
    StatementSupportProviderConfigurationError,
    StatementSupportProviderContractError,
    StatementSupportProviderRefusalError,
    StatementSupportProviderTransientError,
)
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.search.contracts import M1_CONTEXT_CHARACTER_LIMIT

STATEMENT = "A transformer shortage constrained new customer deployments."
EVIDENCE_TEXTS = ("A shortage of high-voltage transformers constrained new customer deployments.",)


@dataclass(slots=True)
class StubResponseUsage:
    """Expose the bounded token totals consumed from an OpenAI response."""

    input_tokens: int = 90
    output_tokens: int = 10
    total_tokens: int = 100


@dataclass(slots=True)
class StubParsedResponse:
    """Expose the parsed response fields consumed by the adapter."""

    model: str = OPENAI_STATEMENT_SUPPORT_MODEL
    status: str = "completed"
    output_parsed: StatementSupportDecision | None = field(
        default_factory=lambda: StatementSupportDecision(supported=True)
    )
    output: tuple[object, ...] = ()
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
    """Expose only the response body and parsed result needed for local diagnostics."""

    body: object
    parse_error: ValidationError | None = None
    parsed_response: StubParsedResponse | None = None

    @property
    def http_response(self) -> StubRawResponse:
        return self

    def parse(self) -> StubParsedResponse:
        if self.parse_error is not None:
            raise self.parse_error
        assert self.parsed_response is not None
        return self.parsed_response

    def json(self) -> object:
        return self.body


@dataclass(slots=True)
class RecordingRawResponsesResource:
    raw_response: StubRawResponse
    calls: list[dict[str, object]] = field(default_factory=list)

    @property
    def with_raw_response(self) -> RecordingRawResponsesResource:
        return self

    def parse(self, **kwargs: object) -> StubRawResponse:
        self.calls.append(kwargs)
        return self.raw_response


@dataclass(slots=True)
class RecordingRawOpenAIClient:
    responses: RecordingRawResponsesResource

    def close(self) -> None:
        pass


def make_evaluator(client: RecordingOpenAIClient) -> OpenAIStatementSupportEvaluator:
    """Inject a recording client through the concrete SDK boundary."""

    return OpenAIStatementSupportEvaluator(client=cast(OpenAI, client))


def make_status_response(status_code: int) -> Response:
    """Build the HTTP response required by concrete SDK status errors."""

    return Response(
        status_code,
        request=Request("POST", "https://api.openai.com/v1/responses"),
    )


@pytest.mark.parametrize("supported", [True, False])
def test_supports_sends_one_bounded_stateless_request(supported: bool) -> None:
    client = RecordingOpenAIClient(
        responses=RecordingResponsesResource(
            response=StubParsedResponse(output_parsed=StatementSupportDecision(supported=supported))
        )
    )

    result = make_evaluator(client).evaluate(STATEMENT, EVIDENCE_TEXTS)

    assert result.decision.supported is supported
    assert result.usage == ProviderUsage(input_units=90, output_units=10)
    assert len(client.responses.calls) == 1
    call = client.responses.calls[0]
    assert call["model"] == OPENAI_STATEMENT_SUPPORT_MODEL
    assert call["text_format"] is StatementSupportDecision
    assert call["reasoning"] == {"effort": "low"}
    assert call["max_output_tokens"] == OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS
    assert call["store"] is False
    assert call["background"] is False
    assert call["tools"] == []
    assert call["truncation"] == "disabled"
    assert call["timeout"] == OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS
    assert "untrusted data" in cast(str, call["instructions"])
    assert json.loads(cast(str, call["input"])) == {
        "evidence_texts": list(EVIDENCE_TEXTS),
        "statement": STATEMENT,
    }


@pytest.mark.parametrize(
    ("statement", "evidence_texts"),
    [
        pytest.param(" ", EVIDENCE_TEXTS, id="blank-statement"),
        pytest.param("x" * 10_001, EVIDENCE_TEXTS, id="oversized-statement"),
        pytest.param(STATEMENT, (), id="missing-evidence"),
        pytest.param(STATEMENT, ("one", "two", "three"), id="too-many-evidence-texts"),
        pytest.param(STATEMENT, (" ",), id="blank-evidence"),
        pytest.param(
            STATEMENT,
            ("x" * (M1_CONTEXT_CHARACTER_LIMIT + 1),),
            id="oversized-evidence",
        ),
    ],
)
def test_invalid_input_is_rejected_before_the_provider_call(
    statement: str,
    evidence_texts: tuple[str, ...],
) -> None:
    client = RecordingOpenAIClient()

    with pytest.raises(ValidationError):
        make_evaluator(client).evaluate(statement, evidence_texts)

    assert client.responses.calls == []


@pytest.mark.parametrize(
    ("response", "application_error"),
    [
        pytest.param(
            StubParsedResponse(model="different-model"),
            StatementSupportProviderContractError,
            id="model-drift",
        ),
        pytest.param(
            StubParsedResponse(status="incomplete", output_parsed=None),
            StatementSupportProviderContractError,
            id="truncated",
        ),
        pytest.param(
            StubParsedResponse(status="failed", output_parsed=None),
            StatementSupportProviderTransientError,
            id="failed",
        ),
        pytest.param(
            StubParsedResponse(output_parsed=None),
            StatementSupportProviderContractError,
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
            StatementSupportProviderRefusalError,
            id="refusal",
        ),
        pytest.param(
            StubParsedResponse(usage=None),
            StatementSupportProviderContractError,
            id="missing-usage",
        ),
        pytest.param(
            StubParsedResponse(usage=StubResponseUsage(total_tokens=101)),
            StatementSupportProviderContractError,
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
            StatementSupportProviderContractError,
            id="usage-out-of-range",
        ),
    ],
)
def test_supports_rejects_non_completed_or_untyped_responses(
    response: StubParsedResponse,
    application_error: type[Exception],
) -> None:
    client = RecordingOpenAIClient(responses=RecordingResponsesResource(response=response))

    with pytest.raises(application_error):
        make_evaluator(client).evaluate(STATEMENT, EVIDENCE_TEXTS)


@pytest.mark.parametrize(
    ("provider_error", "application_error"),
    [
        pytest.param(
            APIConnectionError(
                message="sensitive transport detail",
                request=Request("POST", "https://api.openai.com/v1/responses"),
            ),
            StatementSupportProviderTransientError,
            id="connection",
        ),
        pytest.param(
            RateLimitError(
                "sensitive rate-limit detail",
                response=make_status_response(429),
                body=None,
            ),
            StatementSupportProviderTransientError,
            id="rate-limit",
        ),
        pytest.param(
            InternalServerError(
                "sensitive service detail",
                response=make_status_response(500),
                body=None,
            ),
            StatementSupportProviderTransientError,
            id="service",
        ),
        pytest.param(
            AuthenticationError(
                "sensitive authentication detail",
                response=make_status_response(401),
                body=None,
            ),
            StatementSupportProviderConfigurationError,
            id="authentication",
        ),
        pytest.param(
            APIResponseValidationError(
                response=make_status_response(200),
                body={"sensitive": "provider detail"},
                message="sensitive schema detail",
            ),
            StatementSupportProviderContractError,
            id="response-schema",
        ),
    ],
)
def test_provider_errors_are_translated_without_exposing_details(
    provider_error: Exception,
    application_error: type[Exception],
) -> None:
    client = RecordingOpenAIClient(responses=RecordingResponsesResource(error=provider_error))

    with pytest.raises(application_error) as captured:
        make_evaluator(client).evaluate(STATEMENT, EVIDENCE_TEXTS)

    assert "sensitive" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert len(client.responses.calls) == 1


def test_application_abort_is_not_translated_or_retried() -> None:
    abort = SoftTimeLimitExceeded("synthetic worker deadline")
    client = RecordingOpenAIClient(responses=RecordingResponsesResource(error=abort))

    with pytest.raises(SoftTimeLimitExceeded) as captured:
        make_evaluator(client).evaluate(STATEMENT, EVIDENCE_TEXTS)

    assert captured.value is abort
    assert len(client.responses.calls) == 1


@pytest.mark.parametrize(
    ("model", "operation_timeout_seconds", "max_output_tokens"),
    [
        pytest.param(
            "different-model",
            OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS,
            OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
            id="model",
        ),
        pytest.param(
            OPENAI_STATEMENT_SUPPORT_MODEL,
            0.0,
            OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
            id="timeout",
        ),
        pytest.param(
            OPENAI_STATEMENT_SUPPORT_MODEL,
            OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS,
            OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS + 1,
            id="output-tokens",
        ),
    ],
)
def test_evaluator_rejects_configuration_outside_the_m1_profile(
    model: str,
    operation_timeout_seconds: float,
    max_output_tokens: int,
) -> None:
    with pytest.raises(ValueError):
        OpenAIStatementSupportEvaluator(
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

    evaluator = create_openai_statement_support_evaluator(api_key="synthetic-test-key")
    evaluator.close()

    assert captured == {
        "api_key": "synthetic-test-key",
        "timeout": OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS,
        "max_retries": 0,
    }
    assert client.closed is True


def test_local_debug_shows_only_generated_text_when_support_schema_fails() -> None:
    with pytest.raises(ValidationError) as invalid_output:
        StatementSupportDecision.model_validate({"supported": "not-a-boolean"})
    raw_response = StubRawResponse(
        body={
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": '{"supported":"not-a-boolean"}'},
                    ],
                }
            ],
            "metadata": {"secret": "private provider metadata"},
        },
        parse_error=invalid_output.value,
    )
    client = RecordingRawOpenAIClient(
        responses=RecordingRawResponsesResource(raw_response=raw_response)
    )
    output: list[str] = []

    with pytest.raises(StatementSupportProviderContractError):
        OpenAIStatementSupportEvaluator(
            client=cast(OpenAI, client),
            local_validation_output=output.append,
        ).evaluate(STATEMENT, EVIDENCE_TEXTS)

    assert output == ['{"supported":"not-a-boolean"}']
    assert "private provider metadata" not in output[0]
    assert len(client.responses.calls) == 1


def test_local_debug_shows_parsed_support_decision_on_later_contract_failure() -> None:
    decision = StatementSupportDecision(supported=True)
    raw_response = StubRawResponse(
        body={"metadata": {"secret": "private provider metadata"}},
        parsed_response=StubParsedResponse(output_parsed=decision, usage=None),
    )
    client = RecordingRawOpenAIClient(
        responses=RecordingRawResponsesResource(raw_response=raw_response)
    )
    output: list[str] = []

    with pytest.raises(StatementSupportProviderContractError):
        OpenAIStatementSupportEvaluator(
            client=cast(OpenAI, client),
            local_validation_output=output.append,
        ).evaluate(STATEMENT, EVIDENCE_TEXTS)

    assert output == [decision.model_dump_json()]
