"""Check isolated reranking admission using synthetic values and no runtime resources."""

import os
from typing import TypedDict
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from investment_research.settings import (
    ApiSettings,
    ParserSettings,
    RerankingProviderSettings,
    WorkerSettings,
)
from scripts import m1_5_live_settings as settings

MARKER = "synthetic-reranking-key"


class _Admission(TypedDict):
    monthly_request_limit: int
    starter_allowance_confirmed: bool
    public_synthetic_content_approved: bool


APPROVAL: _Admission = {
    "monthly_request_limit": 6,
    "starter_allowance_confirmed": True,
    "public_synthetic_content_approved": True,
}


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every loader test must explicitly replace local-file access before admission."""

    monkeypatch.setattr(os, "environ", {})

    def forbidden() -> dict[str, str | None]:
        raise AssertionError("test accessed real local configuration")

    monkeypatch.setattr(settings, "_local_values", forbidden)


def test_settings_freeze_model_and_default_to_disabled_without_changing_process_profiles() -> None:
    model = RerankingProviderSettings.model_validate({"api_key": MARKER})
    assert model.monthly_request_limit == 0
    assert model.timeout_seconds == 20
    assert not model.starter_allowance_confirmed
    assert not model.public_synthetic_content_approved
    assert model.model == "bge-reranker-v2-m3"
    assert model.config_version == "m1-5-pinecone-bge-none-v1"
    assert model.billing_policy == "pinecone-starter-bge-included-v1"
    assert MARKER not in repr(model)
    assert MARKER not in model.model_dump_json()
    assert ApiSettings().research_profile == "m1"
    for process in (ApiSettings, WorkerSettings, ParserSettings):
        assert "reranking_provider" not in process.model_fields


@pytest.mark.parametrize("limit", [0, 1, 6, 500])
def test_explicit_quota_and_shorter_deadline_are_valid(limit: int) -> None:
    model = RerankingProviderSettings.model_validate(
        APPROVAL | {"api_key": MARKER, "monthly_request_limit": limit, "timeout_seconds": 1}
    )
    assert model.monthly_request_limit == limit
    assert model.timeout_seconds == 1


@pytest.mark.parametrize("limit", [-1, 501, True, False, 1.5, "6", None])
def test_quota_rejects_expansion_and_implicit_coercion(limit: object) -> None:
    with pytest.raises(ValidationError) as caught:
        RerankingProviderSettings.model_validate(
            APPROVAL | {"api_key": MARKER, "monthly_request_limit": limit}
        )
    assert MARKER not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("starter_allowance_confirmed", False),
        ("public_synthetic_content_approved", False),
        ("starter_allowance_confirmed", "true"),
        ("public_synthetic_content_approved", 1),
        ("timeout_seconds", 0),
        ("timeout_seconds", 21),
        ("timeout_seconds", True),
        ("model", "unapproved-model"),
        ("config_version", "unapproved-configuration"),
        ("billing_policy", "paid-plan"),
        ("endpoint_url", "https://example.invalid"),
    ],
)
def test_unapproved_configuration_is_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError) as caught:
        RerankingProviderSettings.model_validate(APPROVAL | {"api_key": MARKER, field: value})
    assert MARKER not in str(caught.value)


@pytest.mark.parametrize("key", ["", " padded", "padded ", "\n"])
def test_key_must_be_nonempty_and_normalized(key: str) -> None:
    with pytest.raises(ValidationError, match="normalized"):
        RerankingProviderSettings.model_validate(APPROVAL | {"api_key": key})


def test_disabled_loader_never_reads_files_or_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object) -> None:
        raise AssertionError("disabled reranking accessed credentials")

    monkeypatch.setattr(settings, "_credential", forbidden)
    assert settings.load_evaluation_reranking_settings() is None
    assert (
        settings.load_evaluation_reranking_settings(
            monthly_request_limit=0,
            starter_allowance_confirmed=True,
            public_synthetic_content_approved=True,
        )
        is None
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"starter_allowance_confirmed": False},
        {"public_synthetic_content_approved": False},
        {"monthly_request_limit": 501},
        {"monthly_request_limit": True},
    ],
)
def test_rejected_admission_happens_before_configuration_access(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(settings.EvaluationConfigurationError) as caught:
        settings.load_evaluation_reranking_settings(**APPROVAL | overrides)  # type: ignore[arg-type]
    assert str(caught.value) == "setting=reranking_admission, reason=missing_or_invalid"


def test_separate_key_is_loaded_with_process_precedence_and_no_environment_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "_local_values", lambda: {settings.RERANKING_KEY: "synthetic-file-key"}
    )
    os.environ[settings.RERANKING_KEY] = MARKER
    before = os.environ.copy()
    model = settings.load_evaluation_reranking_settings(**APPROVAL)
    assert model is not None and model.api_key.get_secret_value() == MARKER
    assert model.monthly_request_limit == 6
    assert os.environ == before


def test_loader_uses_only_the_dedicated_host_secret_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        f"{settings.RERANKING_KEY}_SECRET_FILE": ".secrets/pinecone_rerank_api_key",
        settings.QUERY_INDEX_KEY: "synthetic-unrelated-query-key",
        settings.INDEX_KEY: "synthetic-unrelated-index-key",
    }
    reader = Mock(return_value=MARKER)
    monkeypatch.setattr(settings, "_local_values", lambda: values)
    monkeypatch.setattr(settings, "_read_secret_file", reader)
    model = settings.load_evaluation_reranking_settings(**APPROVAL)
    assert model is not None and model.api_key.get_secret_value() == MARKER
    reader.assert_called_once_with(settings.RERANKING_KEY, ".secrets/pinecone_rerank_api_key")


@pytest.mark.parametrize("other_key", [settings.QUERY_INDEX_KEY, settings.INDEX_KEY])
def test_missing_inference_key_does_not_reuse_other_roles(
    monkeypatch: pytest.MonkeyPatch, other_key: str
) -> None:
    monkeypatch.setattr(settings, "_local_values", lambda: {other_key: MARKER})
    with pytest.raises(settings.EvaluationConfigurationError) as caught:
        settings.load_evaluation_reranking_settings(**APPROVAL)
    assert str(caught.value) == (
        f"setting={settings.RERANKING_KEY}, reason=credential_missing_or_not_normalized"
    )
    assert MARKER not in str(caught.value)


def test_direct_and_file_key_conflict_is_not_silently_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "_local_values",
        lambda: {
            settings.RERANKING_KEY: MARKER,
            f"{settings.RERANKING_KEY}_SECRET_FILE": ".secrets/pinecone_rerank_api_key",
        },
    )
    with pytest.raises(
        settings.EvaluationConfigurationError, match="direct_and_file_values_conflict"
    ):
        settings.load_evaluation_reranking_settings(**APPROVAL)
