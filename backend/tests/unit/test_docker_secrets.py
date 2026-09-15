"""Tests for disposable file-backed Docker Compose secrets."""

from pathlib import Path

import pytest

from scripts.docker_secrets import (
    COMPOSE_SECRET_FILES,
    ComposeSecretError,
    disposable_compose_secret_environment,
)


def secret_values() -> dict[str, str]:
    """Return a complete set of distinct synthetic values."""

    return {name: f"synthetic-{index}" for index, name in enumerate(COMPOSE_SECRET_FILES)}


def test_disposable_secrets_exist_only_inside_context_with_private_permissions() -> None:
    values = secret_values()

    with disposable_compose_secret_environment(values) as environment:
        paths = tuple(Path(path) for path in environment.values())
        assert set(environment) == {source_name for source_name, _ in COMPOSE_SECRET_FILES.values()}
        for value_name, (source_name, _) in COMPOSE_SECRET_FILES.items():
            path = Path(environment[source_name])
            assert path.read_text(encoding="utf-8") == values[value_name]
            assert path.stat().st_mode & 0o777 == 0o600

    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({}, id="missing"),
        pytest.param(secret_values() | {"UNEXPECTED_SECRET": "marker"}, id="unexpected"),
        pytest.param(secret_values() | {"POSTGRES_PASSWORD": ""}, id="empty"),
        pytest.param(
            secret_values() | {"POSTGRES_PASSWORD": "first-line\nsecond-line"},
            id="multiline",
        ),
    ],
)
def test_disposable_secrets_reject_invalid_input_without_exposing_values(
    values: dict[str, str],
) -> None:
    marker = "first-line"

    with (
        pytest.raises(ComposeSecretError) as error,
        disposable_compose_secret_environment(values),
    ):
        raise AssertionError("invalid secret configuration was accepted")

    assert marker not in str(error.value)
