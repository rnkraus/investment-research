"""Test evaluation configuration with synthetic files, never real credentials or providers."""

import os
from pathlib import Path

import pytest

from scripts import m1_5_live_settings as settings

KEYS = (
    settings.DOCUMENT_KEY,
    settings.INDEX_KEY,
    settings.QUERY_KEY,
    settings.QUERY_INDEX_KEY,
    settings.GENERATION_KEY,
)
MARKER = "synthetic-private-marker"


@pytest.fixture
def configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    root = tmp_path / ".secrets"
    root.mkdir()
    local_file = tmp_path / "synthetic-config.txt"
    local_file.write_text("test sentinel", encoding="utf-8")
    values: dict[str, str | None] = {
        settings.HOST_NAME: "example.pinecone.io",
        settings.BUDGET_NAME: "5000000",
        **{key: f"synthetic-key-{ordinal}" for ordinal, key in enumerate(KEYS)},
    }
    monkeypatch.setattr(settings, "SECRET_ROOT", root)
    monkeypatch.setattr(settings, "ROOT_ENV_FILE", local_file)
    monkeypatch.setattr(settings, "dotenv_values", lambda path: values.copy())
    monkeypatch.setattr(os, "environ", {})
    return values


def test_direct_values_reuse_existing_models_without_loading_other_services(
    configuration: dict[str, str | None],
) -> None:
    configuration.update(
        {
            "POSTGRES_PASSWORD": MARKER,
            "INVESTMENT_RESEARCH_DATABASE_URL": MARKER,
            "INVESTMENT_RESEARCH_REDIS_URL": MARKER,
            "INVESTMENT_RESEARCH_OBJECT_STORAGE_PROVIDER": "s3",
            "INVESTMENT_RESEARCH_RUNTIME_LOCATION": "container",
            "INVESTMENT_RESEARCH_LOCAL_MODEL_FAILURE_OUTPUT": "true",
        }
    )
    before = os.environ.copy()
    worker, api = settings.load_evaluation_settings()
    assert (
        worker.require_indexing_providers().embedding.api_key.get_secret_value()
        == "synthetic-key-0"
    )
    assert api.require_query_providers().pinecone.api_key.get_secret_value() == "synthetic-key-3"
    assert api.require_generation_provider().api_key.get_secret_value() == "synthetic-key-4"
    assert worker.database_url is None and api.redis_url is None
    assert worker.s3 is None and api.local_model_failure_output is False
    assert worker.runtime_location == api.runtime_location == "host"
    assert worker.provider_monthly_budget_micro_usd == 5_000_000
    assert MARKER not in repr((worker, api))
    assert os.environ == before


@pytest.mark.parametrize("absolute", [False, True])
def test_existing_compose_file_references_load_five_separate_roles(
    configuration: dict[str, str | None],
    absolute: bool,
) -> None:
    for ordinal, name in enumerate(KEYS):
        configuration.pop(name)
        path = settings.SECRET_ROOT / f"role-{ordinal}"
        path.write_text(f"synthetic-file-key-{ordinal}\n", encoding="utf-8")
        configuration[f"{name}_SECRET_FILE"] = str(path) if absolute else f".secrets/role-{ordinal}"
    worker, api = settings.load_evaluation_settings()
    assert (
        worker.require_indexing_providers().embedding.api_key.get_secret_value()
        == "synthetic-file-key-0"
    )
    assert (
        worker.require_indexing_providers().pinecone.api_key.get_secret_value()
        == "synthetic-file-key-1"
    )
    assert (
        api.require_query_providers().embedding.api_key.get_secret_value() == "synthetic-file-key-2"
    )
    assert (
        api.require_query_providers().pinecone.api_key.get_secret_value() == "synthetic-file-key-3"
    )
    assert api.require_generation_provider().api_key.get_secret_value() == "synthetic-file-key-4"
    assert "synthetic-file-key" not in repr((worker, api))


def test_process_values_override_file_values_without_environment_mutation(
    configuration: dict[str, str | None],
) -> None:
    os.environ[settings.GENERATION_KEY] = "synthetic-process-key"
    os.environ[settings.BUDGET_NAME] = "1000000"
    before = os.environ.copy()
    _, api = settings.load_evaluation_settings()
    assert api.require_generation_provider().api_key.get_secret_value() == "synthetic-process-key"
    assert api.provider_monthly_budget_micro_usd == 1_000_000
    assert os.environ == before


@pytest.mark.parametrize("name", KEYS)
def test_missing_role_exposes_only_name_and_reason(
    configuration: dict[str, str | None],
    name: str,
) -> None:
    configuration.pop(name)
    with pytest.raises(settings.EvaluationConfigurationError) as caught:
        settings.load_evaluation_settings()
    assert str(caught.value) == f"setting={name}, reason=credential_missing_or_not_normalized"


@pytest.mark.parametrize(
    "mode",
    [
        "conflict",
        "missing",
        "empty",
        "padded",
        "outside",
        "traversal",
        "symlink",
        "directory",
        "oversized",
        "invalid_utf8",
        "secret_root_symlink",
        "fifo",
        "docker_mount",
    ],
)
def test_unsafe_or_invalid_secret_sources_fail_closed_without_values_or_paths(
    configuration: dict[str, str | None],
    tmp_path: Path,
    mode: str,
) -> None:
    name = settings.DOCUMENT_KEY
    configuration.pop(name)
    path = settings.SECRET_ROOT / "document-key"
    configuration[f"{name}_SECRET_FILE"] = str(path)
    if mode == "conflict":
        configuration[name] = MARKER
    elif mode == "empty":
        path.write_text("\n", encoding="utf-8")
    elif mode == "padded":
        path.write_text(f" {MARKER}\n", encoding="utf-8")
    elif mode == "outside":
        outside = tmp_path / MARKER
        outside.write_text(MARKER, encoding="utf-8")
        configuration[f"{name}_SECRET_FILE"] = str(outside)
    elif mode == "traversal":
        configuration[f"{name}_SECRET_FILE"] = f".secrets/../{MARKER}"
    elif mode == "symlink":
        outside = tmp_path / MARKER
        outside.write_text(MARKER, encoding="utf-8")
        path.symlink_to(outside)
    elif mode == "directory":
        path.mkdir()
    elif mode == "oversized":
        path.write_bytes(b"x" * (settings.MAX_SECRET_BYTES + 1))
    elif mode == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif mode == "secret_root_symlink":
        link = tmp_path / "alias"
        link.symlink_to(settings.SECRET_ROOT, target_is_directory=True)
        monkey_path = link / "document-key"
        configuration[f"{name}_SECRET_FILE"] = str(monkey_path)
    elif mode == "fifo":
        os.mkfifo(path)
    elif mode == "docker_mount":
        os.environ[f"{name}_FILE"] = f"/run/secrets/{MARKER}"
    with pytest.raises(settings.EvaluationConfigurationError) as caught:
        settings.load_evaluation_settings()
    assert f"setting={name}, reason=" in str(caught.value)
    assert MARKER not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize(
    "name,value",
    [
        (settings.HOST_NAME, None),
        (settings.HOST_NAME, f"https://{MARKER}.pinecone.io"),
        (settings.BUDGET_NAME, None),
        (settings.BUDGET_NAME, "0"),
        (settings.BUDGET_NAME, "5000001"),
        (settings.BUDGET_NAME, MARKER),
    ],
)
def test_host_and_budget_validation_preserve_safe_errors(
    configuration: dict[str, str | None],
    name: str,
    value: str | None,
) -> None:
    configuration[name] = value
    with pytest.raises(settings.EvaluationConfigurationError) as caught:
        settings.load_evaluation_settings()
    assert str(caught.value) == f"setting={name}, reason=missing_or_invalid"
    assert MARKER not in str(caught.value)
