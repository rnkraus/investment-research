"""Offline tests through the locked SDK inference transport, not a live provider."""

import asyncio
import json
from collections.abc import Callable
from copy import deepcopy
from typing import cast
from uuid import UUID

import httpx
import pytest
from pinecone import AsyncPinecone
from pydantic import SecretStr

from investment_research.infrastructure.search.pinecone_reranking import (
    PINECONE_RERANK_CANDIDATE_LIMIT,
    PINECONE_RERANK_CONFIG_VERSION,
    PINECONE_RERANK_PASSAGE_CHARACTER_LIMIT,
    PINECONE_RERANK_QUERY_CHARACTER_LIMIT,
    PINECONE_RERANK_TOTAL_CHARACTER_LIMIT,
    PineconeReranker,
    create_pinecone_reranker,
)
from investment_research.modules.search.contracts import RerankCandidate, RerankRequest
from investment_research.modules.search.ports import (
    Reranker,
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderTransientError,
)

TEST_KEY = "offline-pinecone-credential"
QUERY = "what constrained delivery?"
TEXT = "Synthetic transformers constrained the scheduled delivery."


def make_request(count: int = 3, *, text: str = TEXT, query: str = QUERY) -> RerankRequest:
    return RerankRequest(
        normalized_query=query,
        candidates=tuple(
            RerankCandidate(retrieval_candidate_id=UUID(int=index + 1), text=text)
            for index in range(count)
        ),
        reranker_config_version=PINECONE_RERANK_CONFIG_VERSION,
    )


def provider_body() -> dict[str, object]:
    return {
        "model": "bge-reranker-v2-m3",
        "data": [
            {"index": 2, "score": 0.8},
            {"index": 1, "score": 0.8},
            {"index": 0, "score": 0.2},
        ],
        "usage": {"rerank_units": 1},
    }


def install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[dict[str, object]]:
    """Replace only network I/O; retain SDK serialization, parsing, and retry transport."""

    options: list[dict[str, object]] = []

    def create_transport(**kwargs: object) -> httpx.MockTransport:
        options.append(kwargs)
        return httpx.MockTransport(handler)

    monkeypatch.setattr("pinecone._internal.http_client.httpx.AsyncHTTPTransport", create_transport)
    return options


def test_sdk_request_ranking_usage_and_content_free_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=provider_body())

    options = install_transport(monkeypatch, handler)
    caplog.set_level("DEBUG")
    adapter: Reranker = create_pinecone_reranker(api_key=SecretStr(TEST_KEY))
    request = make_request()
    result = adapter.rerank(request)

    assert [item.retrieval_candidate_id for item in result.results] == [
        request.candidates[index].retrieval_candidate_id for index in (1, 2, 0)
    ]
    assert [item.rank for item in result.results] == [1, 2, 3]
    assert result.usage.input_units == 1
    assert result.usage.output_units == 0
    assert result.model == "bge-reranker-v2-m3"
    assert result.reranker_config_version == PINECONE_RERANK_CONFIG_VERSION
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.pinecone.io/rerank"
    body = json.loads(requests[0].content)
    assert body == {
        "model": "bge-reranker-v2-m3",
        "query": QUERY,
        "documents": [{"text": TEXT} for _ in range(3)],
        "rank_fields": ["text"],
        "return_documents": False,
        "top_n": 3,
        "parameters": {"truncate": "NONE"},
    }
    assert options[0]["verify"] is True
    assert cast(httpx.Limits, options[0]["limits"]).max_connections == 1
    assert requests[0].extensions["timeout"] == {
        "connect": 20.0,
        "read": 20.0,
        "write": 20.0,
        "pool": 20.0,
    }
    for value in (TEST_KEY, QUERY, TEXT, str(request.candidates[0].retrieval_candidate_id)):
        assert value not in caplog.text
        assert value not in repr(adapter)


@pytest.mark.parametrize("status", [408, 409, 429, 500, 503])
def test_transient_http_errors_make_exactly_one_attempt(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"message": f"{TEST_KEY} {TEXT}"})

    install_transport(monkeypatch, handler)
    adapter = create_pinecone_reranker(api_key=SecretStr(TEST_KEY))
    with pytest.raises(SearchProviderTransientError) as caught:
        adapter.rerank(make_request())
    assert calls == 1
    assert TEST_KEY not in str(caught.value)
    assert TEXT not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_configuration_errors_are_safe_and_not_retried(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"message": f"{TEST_KEY} {TEXT}"})

    install_transport(monkeypatch, handler)
    with pytest.raises(SearchProviderConfigurationError) as caught:
        create_pinecone_reranker(api_key=SecretStr(TEST_KEY)).rerank(make_request())
    assert calls == 1
    assert TEST_KEY not in str(caught.value)
    assert TEXT not in str(caught.value)


@pytest.mark.parametrize("failure", ["connect", "read_timeout"])
def test_transport_errors_are_safe_and_not_retried(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "connect":
            raise httpx.ConnectError(f"{TEST_KEY} {TEXT}", request=request)
        raise httpx.ReadTimeout(f"{TEST_KEY} {TEXT}", request=request)

    install_transport(monkeypatch, handler)
    with pytest.raises(SearchProviderTransientError) as caught:
        create_pinecone_reranker(api_key=SecretStr(TEST_KEY)).rerank(make_request())
    assert calls == 1
    assert TEST_KEY not in str(caught.value)
    assert TEXT not in str(caught.value)


@pytest.mark.parametrize(
    "replacement",
    [
        {"model": "unexpected-model"},
        {"data": []},
        {"data": [{"index": 0, "score": 0.5}]},
        {"data": [{"index": index, "score": 0.5} for index in (0, 0, 2)]},
        {"data": [{"index": index, "score": 0.5} for index in (0, 1, 3)]},
        {"data": [{"index": index, "score": 0.5} for index in (-1, 1, 2)]},
        {"data": [{"index": index, "score": 0.5} for index in (True, 1, 2)]},
        {"data": [{"index": index, "score": "0.5"} for index in range(3)]},
        {"data": [{"index": index, "score": True} for index in range(3)]},
        {
            "data": [
                {"index": index, "score": 0.5, "document": {"text": TEXT}} for index in range(3)
            ]
        },
        {"usage": {"rerank_units": 0}},
        {"usage": {"rerank_units": -1}},
        {"usage": {"rerank_units": True}},
        {"usage": {"rerank_units": "1"}},
        {"usage": {"rerank_units": 9_223_372_036_854_775_808}},
        {"usage": {}},
    ],
)
def test_rejects_malformed_or_incomplete_sdk_responses(
    monkeypatch: pytest.MonkeyPatch, replacement: dict[str, object]
) -> None:
    body = deepcopy(provider_body())
    body.update(replacement)
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=body))
    with pytest.raises(SearchProviderContractError):
        create_pinecone_reranker(api_key=SecretStr(TEST_KEY)).rerank(make_request())


@pytest.mark.parametrize("score", ["NaN", "Infinity", "-Infinity"])
def test_rejects_non_finite_scores(monkeypatch: pytest.MonkeyPatch, score: str) -> None:
    body = (
        '{"model":"bge-reranker-v2-m3","data":['
        f'{{"index":0,"score":{score}}}],"usage":{{"rerank_units":1}}}}'
    )
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=body))
    with pytest.raises(SearchProviderContractError):
        create_pinecone_reranker(api_key=SecretStr(TEST_KEY)).rerank(make_request(1))


@pytest.mark.parametrize("kind", ["candidates", "query", "passage", "total", "version"])
def test_rejects_invalid_input_before_creating_a_client(kind: str) -> None:
    def forbidden_factory() -> AsyncPinecone:
        pytest.fail("over-limit input must not construct a client")

    adapter = PineconeReranker(client_factory=forbidden_factory)
    if kind == "candidates":
        request = make_request(PINECONE_RERANK_CANDIDATE_LIMIT + 1)
    elif kind == "query":
        request = make_request(query="q" * (PINECONE_RERANK_QUERY_CHARACTER_LIMIT + 1))
    elif kind == "passage":
        request = make_request(text="x" * (PINECONE_RERANK_PASSAGE_CHARACTER_LIMIT + 1))
    elif kind == "total":
        request = make_request(11, text="x" * PINECONE_RERANK_PASSAGE_CHARACTER_LIMIT)
        assert sum(len(item.text) for item in request.candidates) > (
            PINECONE_RERANK_TOTAL_CHARACTER_LIMIT
        )
    else:
        request = make_request().model_copy(update={"reranker_config_version": "other-version"})
    error = SearchProviderConfigurationError if kind == "version" else SearchProviderContractError
    with pytest.raises(error):
        adapter.rerank(request)


def test_total_deadline_cancels_http_operation_and_allows_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    cancelled = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls, cancelled
        calls += 1
        if calls == 1:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled = True
                raise
        return httpx.Response(200, json=provider_body())

    monkeypatch.setattr(
        "pinecone._internal.http_client.httpx.AsyncHTTPTransport",
        lambda **kwargs: httpx.MockTransport(handler),
    )
    adapter = create_pinecone_reranker(api_key=SecretStr(TEST_KEY), operation_timeout_seconds=0.02)
    with pytest.raises(SearchProviderTransientError):
        adapter.rerank(make_request())
    assert cancelled
    assert calls == 1
    assert len(adapter.rerank(make_request()).results) == 3
    assert calls == 2


@pytest.mark.parametrize("status", [200, 401, 503])
def test_sdk_transport_is_closed_after_success_and_failure(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    closed = 0

    class ClosingTransport(httpx.MockTransport):
        async def aclose(self) -> None:
            nonlocal closed
            closed += 1
            await super().aclose()

    monkeypatch.setattr(
        "pinecone._internal.http_client.httpx.AsyncHTTPTransport",
        lambda **kwargs: ClosingTransport(
            lambda request: httpx.Response(status, json=provider_body())
        ),
    )
    adapter = create_pinecone_reranker(api_key=SecretStr(TEST_KEY))
    if status == 200:
        adapter.rerank(make_request())
    else:
        with pytest.raises((SearchProviderConfigurationError, SearchProviderTransientError)):
            adapter.rerank(make_request())
    assert closed == 1


def test_rejects_late_success_after_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=provider_body()))
    times = iter((0.0, 21.0))
    monkeypatch.setattr(
        "investment_research.infrastructure.search.pinecone_reranking.monotonic",
        lambda: next(times),
    )
    with pytest.raises(SearchProviderTransientError):
        create_pinecone_reranker(api_key=SecretStr(TEST_KEY)).rerank(make_request())


def test_unexpected_programming_error_is_not_hidden_as_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("synthetic programming defect")

    install_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="programming defect"):
        create_pinecone_reranker(api_key=SecretStr(TEST_KEY)).rerank(make_request())


def test_inflight_call_is_rejected_without_waiting_or_network() -> None:
    adapter = create_pinecone_reranker(api_key=SecretStr(TEST_KEY))
    assert adapter._inflight.acquire(blocking=False)
    try:
        with pytest.raises(SearchProviderTransientError, match="already in progress"):
            adapter.rerank(make_request())
    finally:
        adapter._inflight.release()


def test_active_event_loop_is_rejected_before_network() -> None:
    async def call() -> None:
        adapter = create_pinecone_reranker(api_key=SecretStr(TEST_KEY))
        with pytest.raises(SearchProviderConfigurationError, match="synchronous execution"):
            adapter.rerank(make_request())

    asyncio.run(call())


@pytest.mark.parametrize("value", [0, -1, 21, float("inf"), float("nan")])
def test_rejects_invalid_operation_timeout(value: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        create_pinecone_reranker(api_key=SecretStr(TEST_KEY), operation_timeout_seconds=value)


def test_rejects_empty_credential_without_environment_fallback() -> None:
    with pytest.raises(SearchProviderConfigurationError, match="must not be empty"):
        create_pinecone_reranker(api_key=SecretStr(" "))


@pytest.mark.parametrize("name", ["PINECONE_DEBUG_CURL", "PINECONE_ADDITIONAL_HEADERS"])
def test_rejects_content_logging_and_header_overrides(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    adapter = create_pinecone_reranker(api_key=SecretStr(TEST_KEY))
    monkeypatch.setenv(name, "offline-unsafe-override")
    with pytest.raises(SearchProviderConfigurationError, match="overrides"):
        adapter.rerank(make_request())
    with pytest.raises(SearchProviderConfigurationError, match="overrides"):
        create_pinecone_reranker(api_key=SecretStr(TEST_KEY))
