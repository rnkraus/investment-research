"""Offline tests for the complete, non-secret local TOML tuning contract."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from investment_research import settings as settings_module
from investment_research.rag_settings import RagConfigurationError, RagSettings, load_rag_settings

TOML = """
[retrieval]
lexical_candidate_limit = 12
dense_candidate_limit = 16
[context]
chunk_limit = 16
character_limit = 12000
[generation]
input_overhead_limit = 18000
"""


def test_loads_all_five_tuning_values(tmp_path: Path) -> None:
    path = tmp_path / "rag.toml"
    path.write_text(TOML, encoding="utf-8")
    settings = load_rag_settings(path)
    assert settings.retrieval.lexical_candidate_limit == 12
    assert settings.retrieval.dense_candidate_limit == 16
    assert settings.context.chunk_limit == 16
    assert settings.context.character_limit == 12_000
    assert settings.generation.input_overhead_limit == 18_000


def test_programmatic_settings_keep_frozen_defaults() -> None:
    settings = RagSettings()
    assert settings.retrieval.lexical_candidate_limit == 20
    assert settings.retrieval.dense_candidate_limit == 20
    assert settings.context.chunk_limit == 8
    assert settings.context.character_limit == 24_000
    assert settings.generation.input_overhead_limit == 16_000


def test_repository_config_has_a_valid_live_overhead() -> None:
    path = Path(__file__).resolve().parents[3] / "config" / "rag.toml"
    assert 16_000 <= load_rag_settings(path).generation.input_overhead_limit <= 18_000


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("lexical_candidate_limit = 12", "lexical_candidate_limit = 21"),
        ("dense_candidate_limit = 16", "dense_candidate_limit = 0"),
        ("chunk_limit = 16", "chunk_limit = 33"),
        ("chunk_limit = 16", "chunk_limit = 0"),
        ("chunk_limit = 16", "chunk_limit = true"),
        ("chunk_limit = 16", 'chunk_limit = "16"'),
        ("chunk_limit = 16", "chunk_limit = 16.0"),
        ("character_limit = 12000", "character_limit = 24001"),
        ("character_limit = 12000", "character_limit = 0"),
        ("input_overhead_limit = 18000", "input_overhead_limit = 18001"),
        ("input_overhead_limit = 18000", "input_overhead_limit = 15999"),
        ("input_overhead_limit = 18000", "input_overhead_limit = true"),
        ("input_overhead_limit = 18000", 'input_overhead_limit = "18000"'),
        ("input_overhead_limit = 18000", "input_overhead_limit = 18000.0"),
        ("input_overhead_limit = 18000", "input_overhead_units = 18000"),
        ("[generation]\ninput_overhead_limit = 18000", ""),
        ("chunk_limit = 16", "chunks = 16"),
        ("[context]", "[contex]"),
        ("character_limit = 12000", 'character_limit = 12000\napi_key = "private-test-marker"'),
        ("[context]", 'unexpected = "private-test-marker"\n[context]'),
        ("chunk_limit = 16", "chunk_limit = ["),
    ],
)
def test_invalid_values_fail_without_echoing_contents(
    tmp_path: Path, original: str, replacement: str
) -> None:
    path = tmp_path / "rag.toml"
    path.write_text(TOML.replace(original, replacement), encoding="utf-8")
    with pytest.raises(RagConfigurationError) as failure:
        load_rag_settings(path)
    assert "private-test-marker" not in str(failure.value)


def test_missing_file_fails_without_a_default_fallback(tmp_path: Path) -> None:
    with pytest.raises(RagConfigurationError, match="missing"):
        load_rag_settings(tmp_path / "missing.toml")


def test_api_loader_uses_the_supplied_config_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "rag.toml"
    path.write_text(TOML, encoding="utf-8")
    # Replace only the environment reader: no environment or credential file is accessed.
    monkeypatch.setattr(settings_module, "_read_local_values", lambda _: {})
    monkeypatch.setattr(
        settings_module, "_configuration_value", lambda _name, _values, default=None: default
    )
    monkeypatch.setattr(settings_module, "_database_runtime_values", lambda _: {})
    monkeypatch.setattr(settings_module, "_queue_runtime_values", lambda *_: {})
    monkeypatch.setattr(settings_module, "_object_storage_runtime_values", lambda _: {})
    monkeypatch.setattr(settings_module, "_provider_budget_runtime_values", lambda _: {})
    monkeypatch.setattr(settings_module, "_dense_provider_runtime_values", lambda *_, **__: None)
    monkeypatch.setattr(settings_module, "_generation_provider_runtime_values", lambda _: None)

    settings = settings_module.load_api_settings(env_file=None, rag_config_file=path)
    assert settings.rag == load_rag_settings(path)


def test_invalid_config_fails_before_environment_or_resource_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reader = Mock(side_effect=AssertionError("must not read environment"))
    monkeypatch.setattr(settings_module, "_read_local_values", reader)
    with pytest.raises(RagConfigurationError):
        settings_module.load_api_settings(env_file=None, rag_config_file=tmp_path / "missing.toml")
    reader.assert_not_called()
