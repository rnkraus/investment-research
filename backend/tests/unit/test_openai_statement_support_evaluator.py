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
from investment_research.modules.evidence.contracts import (
    ContextualStatementSupportInput,
    StatementSupportDecision,
    StatementSupportResult,
)
from investment_research.modules.evidence.ports import (
    StatementSupportProviderConfigurationError,
    StatementSupportProviderContractError,
    StatementSupportProviderRefusalError,
    StatementSupportProviderTransientError,
)
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.search.contracts import M1_CONTEXT_CHARACTER_LIMIT
from tests.unit.test_statement_support_context import (
    make_document_source_and_reference,
    make_source_and_reference,
    prepare,
    prepare_document,
)

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
    output_parsed: StatementSupportDecision | openai_module._ExplainedSupportDecision | None = (
        field(default_factory=lambda: StatementSupportDecision(supported=True))
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
    assert result.diagnostic_reason is None
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


def contextual_input() -> ContextualStatementSupportInput:
    source, reference = make_source_and_reference()
    request = prepare(source, reference)
    assert request is not None
    return request


@pytest.mark.parametrize("supported", [False, True])
def test_document_attribution_sends_checked_origin_and_separates_facts(supported: bool) -> None:
    client = RecordingOpenAIClient(
        responses=RecordingResponsesResource(
            response=StubParsedResponse(output_parsed=StatementSupportDecision(supported=supported))
        )
    )
    request = prepare_document(*make_document_source_and_reference())
    assert request is not None
    result = OpenAIStatementSupportEvaluator(
        client=cast(OpenAI, client),
        include_attribution_context=True,
        include_document_attribution_context=True,
    ).evaluate_contextual(request)

    assert result.decision.supported is supported
    assert len(client.responses.calls) == 1
    call = client.responses.calls[0]
    item = request.evidence[0]
    assert json.loads(cast(str, call["input"])) == {
        "statement": request.statement,
        "evidence": [
            {
                "document_id": str(item.reference.document_id),
                "document_version_id": str(item.reference.document_version_id),
                "structure_block_id": str(item.reference.structure_block_id),
                "exact_text": item.reference.exact_text,
                "attribution_context": item.attribution_context.text,
                "attribution_structure_block_id": str(item.attribution_context.structure_block_id),
                "attribution_scope": "same_document_version",
            }
        ],
    }
    assert item.reference.structure_block_id != item.attribution_context.structure_block_id
    assert call["text_format"] is StatementSupportDecision
    assert call["tools"] == [] and call["store"] is False
    assert call["timeout"] == OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS
    assert call["instructions"] == openai_module._document_attribution_instructions(
        explanations=False
    )
    assert len(cast(str, call["input"]).encode("utf-8")) <= len(
        request.model_dump_json().encode("utf-8")
    )


@pytest.mark.parametrize("document_policy", [False, True])
def test_same_block_and_document_profiles_cannot_be_silently_interchanged(
    document_policy: bool,
) -> None:
    request = (
        contextual_input()
        if document_policy
        else prepare_document(*make_document_source_and_reference())
    )
    assert request is not None
    client = RecordingOpenAIClient()

    with pytest.raises(StatementSupportProviderConfigurationError):
        OpenAIStatementSupportEvaluator(
            client=cast(OpenAI, client),
            include_attribution_context=True,
            include_document_attribution_context=document_policy,
        ).evaluate_contextual(request)
    assert not client.responses.calls


@pytest.mark.parametrize("supported", [False, True])
def test_contextual_support_sends_only_source_paired_allowlisted_fields(supported: bool) -> None:
    client = RecordingOpenAIClient(
        responses=RecordingResponsesResource(
            response=StubParsedResponse(output_parsed=StatementSupportDecision(supported=supported))
        )
    )
    evaluator = OpenAIStatementSupportEvaluator(
        client=cast(OpenAI, client), include_attribution_context=True
    )
    request = contextual_input()
    result = evaluator.evaluate_contextual(request)

    assert result.decision.supported is supported
    assert result.diagnostic_reason is None
    assert result.usage == ProviderUsage(input_units=90, output_units=10)
    assert len(client.responses.calls) == 1
    call = client.responses.calls[0]
    payload = json.loads(cast(str, call["input"]))
    assert payload["statement"] == request.statement
    reference = request.evidence[0].reference
    assert payload["evidence"] == [
        {
            "document_id": str(reference.document_id),
            "document_version_id": str(reference.document_version_id),
            "structure_block_id": str(reference.structure_block_id),
            "exact_text": reference.exact_text,
            "attribution_context": request.evidence[0].attribution_context.text,
        }
    ]
    instructions = cast(str, call["instructions"])
    assert "untrusted data" in instructions
    assert "Context cannot fill missing operational facts" in instructions
    assert "Do not transfer" in instructions
    assert call["text_format"] is StatementSupportDecision
    assert call["store"] is False and call["background"] is False
    assert call["tools"] == []
    assert call["timeout"] == OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS
    assert len(cast(str, call["input"]).encode("utf-8")) <= len(
        request.model_dump_json().encode("utf-8")
    )


def test_support_profiles_cannot_be_silently_interchanged() -> None:
    client = RecordingOpenAIClient()
    with pytest.raises(StatementSupportProviderConfigurationError):
        make_evaluator(client).evaluate_contextual(contextual_input())
    with pytest.raises(StatementSupportProviderConfigurationError):
        OpenAIStatementSupportEvaluator(
            client=cast(OpenAI, client), include_attribution_context=True
        ).evaluate(STATEMENT, EVIDENCE_TEXTS)
    assert not client.responses.calls


def test_contextual_invalid_input_is_rejected_before_any_sdk_call() -> None:
    client = RecordingOpenAIClient()
    request = contextual_input().model_copy(update={"evidence": ()})
    with pytest.raises(ValidationError):
        OpenAIStatementSupportEvaluator(
            client=cast(OpenAI, client), include_attribution_context=True
        ).evaluate_contextual(request)
    assert not client.responses.calls


@pytest.mark.parametrize("document_policy", [False, True])
def test_contextual_local_reason_uses_same_decision_contract_and_one_request(
    document_policy: bool,
) -> None:
    parsed = StubParsedResponse(
        output_parsed=openai_module._ExplainedSupportDecision(
            supported=False, reason="The quote lacks the claimed quantity."
        )
    )
    client = RecordingRawOpenAIClient(
        RecordingRawResponsesResource(StubRawResponse(body={}, parsed_response=parsed))
    )
    evaluator = OpenAIStatementSupportEvaluator(
        client=cast(OpenAI, client),
        include_attribution_context=True,
        include_document_attribution_context=document_policy,
        include_decision_reason=True,
        local_validation_output=lambda _: None,
    )
    request = (
        prepare_document(*make_document_source_and_reference())
        if document_policy
        else contextual_input()
    )
    assert request is not None
    result = evaluator.evaluate_contextual(request)
    assert result.decision.supported is False
    assert result.diagnostic_reason == "The quote lacks the claimed quantity."
    assert "diagnostic_reason" not in result.model_dump()
    assert len(client.responses.calls) == 1
    assert client.responses.calls[0]["text_format"] is openai_module._ExplainedSupportDecision
    if document_policy:
        assert client.responses.calls[0][
            "instructions"
        ] == openai_module._document_attribution_instructions(explanations=True)


def test_document_profile_configuration_is_rejected_before_sdk_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(**kwargs: object) -> None:
        pytest.fail("invalid context configuration must not construct an SDK client")

    monkeypatch.setattr(openai_module, "OpenAI", forbidden)
    with pytest.raises(ValueError, match="contextual support profile"):
        create_openai_statement_support_evaluator(
            api_key="synthetic-test-key", include_document_attribution_context=True
        )


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


@pytest.mark.parametrize("include_reason", [False, True])
def test_factory_disables_sdk_retries_and_close_releases_client(
    monkeypatch: pytest.MonkeyPatch,
    include_reason: bool,
) -> None:
    captured: dict[str, object] = {}
    client = RecordingOpenAIClient()

    def recording_constructor(**kwargs: object) -> RecordingOpenAIClient:
        captured.update(kwargs)
        return client

    monkeypatch.setattr(openai_module, "OpenAI", recording_constructor)

    output: list[str] = []
    evaluator = create_openai_statement_support_evaluator(
        api_key="synthetic-test-key",
        include_decision_reason=include_reason,
        local_validation_output=output.append if include_reason else None,
    )
    assert evaluator.include_decision_reason is include_reason
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


@pytest.mark.parametrize("supported", [True, False])
def test_local_support_reason_preserves_decision_and_never_serializes_diagnostic(
    supported: bool,
) -> None:
    reason = "Synthetic diagnostic that grants no authority."
    decision = openai_module._ExplainedSupportDecision(supported=supported, reason=reason)
    client = RecordingRawOpenAIClient(
        responses=RecordingRawResponsesResource(
            raw_response=StubRawResponse(
                body={"metadata": {"secret": "private provider metadata"}},
                parsed_response=StubParsedResponse(output_parsed=decision),
            )
        )
    )
    output: list[str] = []

    result = OpenAIStatementSupportEvaluator(
        client=cast(OpenAI, client),
        local_validation_output=output.append,
        include_decision_reason=True,
    ).evaluate(STATEMENT, EVIDENCE_TEXTS)

    assert result.decision == StatementSupportDecision(supported=supported)
    assert result.diagnostic_reason == reason
    assert result.usage == ProviderUsage(input_units=90, output_units=10)
    assert result.model_dump() == {
        "decision": {"supported": supported},
        "usage": {"input_units": 90, "output_units": 10},
    }
    assert reason not in result.model_dump_json()
    assert reason not in repr(result)
    assert output == []  # Successful diagnostics are displayed by the opted-in test wrapper.
    assert len(client.responses.calls) == 1
    call = client.responses.calls[0]
    assert call["text_format"] is openai_module._ExplainedSupportDecision
    assert "short English reason" in cast(str, call["instructions"])
    assert "hidden reasoning" in cast(str, call["instructions"])
    assert json.loads(cast(str, call["input"])) == {
        "evidence_texts": list(EVIDENCE_TEXTS),
        "statement": STATEMENT,
    }
    assert call["max_output_tokens"] == OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS
    assert call["timeout"] == OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS
    assert call["store"] is False
    assert call["tools"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"supported": False},
        {"supported": False, "reason": ""},
        {"supported": False, "reason": " \n "},
        {"supported": False, "reason": "x" * 501},
        {"supported": False, "reason": None},
        {"supported": "false", "reason": "Synthetic reason."},
        {"supported": False, "reason": "Synthetic reason.", "extra": "not allowed"},
    ],
)
def test_local_reason_schema_rejects_invalid_diagnostics_without_exposing_input(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError) as error:
        openai_module._ExplainedSupportDecision.model_validate(payload)
    assert "input_value=" not in str(error.value)


def test_local_reason_schema_requires_both_fields_and_has_no_extra_properties() -> None:
    schema = openai_module._ExplainedSupportDecision.model_json_schema()
    assert schema["required"] == ["supported", "reason"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["reason"]["maxLength"] == 500
    assert set(StatementSupportDecision.model_json_schema()["properties"]) == {"supported"}


def test_local_reason_mode_requires_explicit_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="local diagnostic callback"):
        OpenAIStatementSupportEvaluator(
            client=cast(OpenAI, RecordingOpenAIClient()), include_decision_reason=True
        )

    def forbidden(**kwargs: object) -> None:
        pytest.fail("invalid diagnostic configuration must not construct an SDK client")

    monkeypatch.setattr(openai_module, "OpenAI", forbidden)
    with pytest.raises(ValueError, match="local diagnostic callback"):
        create_openai_statement_support_evaluator(
            api_key="synthetic-test-key", include_decision_reason=True
        )


@pytest.mark.parametrize("include_reason", [True, False])
def test_provider_cannot_silently_switch_support_schema(include_reason: bool) -> None:
    parsed = (
        StatementSupportDecision(supported=True)
        if include_reason
        else openai_module._ExplainedSupportDecision(supported=True, reason="Synthetic reason.")
    )
    client = RecordingRawOpenAIClient(
        responses=RecordingRawResponsesResource(
            raw_response=StubRawResponse(
                body={}, parsed_response=StubParsedResponse(output_parsed=parsed)
            )
        )
    )
    with pytest.raises(StatementSupportProviderContractError):
        OpenAIStatementSupportEvaluator(
            client=cast(OpenAI, client),
            local_validation_output=lambda text: None,
            include_decision_reason=include_reason,
        ).evaluate(STATEMENT, EVIDENCE_TEXTS)


def test_support_result_without_diagnostic_stays_backward_compatible() -> None:
    result = StatementSupportResult(
        decision=StatementSupportDecision(supported=True),
        usage=ProviderUsage(input_units=1, output_units=0),
    )
    assert result.diagnostic_reason is None
    assert "diagnostic_reason" not in result.model_dump_json()
