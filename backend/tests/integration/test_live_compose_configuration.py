"""Render live Compose policy wiring without a daemon, real secrets, or provider calls."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.docker_secrets import COMPOSE_SECRET_FILES, disposable_compose_secret_environment

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROFILE_VARIABLE = "INVESTMENT_RESEARCH_RESEARCH_PROFILE"
PROVIDER_ROLES = (
    "OPENAI_QUERY",
    "PINECONE_QUERY",
    "OPENAI_GENERATION",
    "OPENAI_DOCUMENT",
    "PINECONE_INDEX",
)


def render_configuration(tmp_path: Path, *, live: bool, profile: str | None) -> dict[str, Any]:
    """Use a clean environment and disposable synthetic files to render actual Compose YAML."""

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is required for configuration rendering")
    provider_placeholder = tmp_path / "synthetic-provider-placeholder"
    provider_placeholder.write_text("synthetic-config-only-value", encoding="utf-8")
    provider_placeholder.chmod(0o600)
    command = [docker, "compose", "--file", str(REPOSITORY_ROOT / "compose.yaml")]
    if live:
        command.extend(["--file", str(REPOSITORY_ROOT / "compose.m1-live.yaml")])
    command.extend(["config", "--format", "json"])

    with disposable_compose_secret_environment(
        dict.fromkeys(COMPOSE_SECRET_FILES, "synthetic-config-only-value")
    ) as secret_paths:
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "COMPOSE_DISABLE_ENV_FILE": "1",
            "POSTGRES_DB": "synthetic_research",
            "POSTGRES_USER": "synthetic_user",
            "INVESTMENT_RESEARCH_S3_BUCKET": "synthetic-config-only",
            "INVESTMENT_RESEARCH_PROVIDER_MONTHLY_BUDGET_MICRO_USD": "5000000",
            "INVESTMENT_RESEARCH_PINECONE_INDEX_HOST": "synthetic-index.example.svc.pinecone.io",
            **secret_paths,
            **{
                f"INVESTMENT_RESEARCH_{role}_API_KEY_SECRET_FILE": str(provider_placeholder)
                for role in PROVIDER_ROLES
            },
        }
        if profile is not None:
            environment[PROFILE_VARIABLE] = profile
        result = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    return cast(dict[str, Any], json.loads(result.stdout))


@pytest.mark.parametrize(
    ("live", "profile", "expected"),
    [
        (False, "m1-5-citable-units", None),
        (True, None, "m1-5-citable-units"),
        (True, "m1", "m1"),
        (True, "m1-5-citable-units", "m1-5-citable-units"),
    ],
    ids=["base-unaffected", "live-default", "live-m1", "live-citable-units"],
)
def test_compose_live_defaults_to_citable_units_and_keeps_profile_api_only(
    tmp_path: Path, live: bool, profile: str | None, expected: str | None
) -> None:
    services = render_configuration(tmp_path, live=live, profile=profile)["services"]
    assert services["api"]["environment"].get(PROFILE_VARIABLE) == expected
    for name, service in services.items():
        environment = service.get("environment", {})
        if name != "api":
            assert PROFILE_VARIABLE not in environment
        for role in PROVIDER_ROLES:
            assert f"INVESTMENT_RESEARCH_{role}_API_KEY" not in environment
    for name in ("api", "web"):
        assert all(port["host_ip"] == "127.0.0.1" for port in services[name]["ports"])
    rag_mounts = [
        mount for mount in services["api"]["volumes"] if mount["target"] == "/config/rag.toml"
    ]
    assert len(rag_mounts) == 1
    assert rag_mounts[0]["source"] == str(REPOSITORY_ROOT / "config" / "rag.toml")
    assert rag_mounts[0]["read_only"] is True
    # Compose versions may omit false-valued fields when serializing the model as JSON.
    assert rag_mounts[0].get("bind", {}).get("create_host_path", False) is False
    for name, service in services.items():
        if name != "api":
            assert all(
                mount["target"] != "/config/rag.toml" for mount in service.get("volumes", [])
            )
    if live:
        assert (
            services["api"]["environment"]["INVESTMENT_RESEARCH_OPENAI_GENERATION_API_KEY_FILE"]
            == "/run/secrets/openai_generation_api_key"
        )
        assert (
            services["worker"]["environment"]["INVESTMENT_RESEARCH_OPENAI_DOCUMENT_API_KEY_FILE"]
            == "/run/secrets/openai_document_api_key"
        )
        assert not any("API_KEY" in name for name in services["parser"]["environment"])


@pytest.mark.parametrize("bind", [{}, {"create_host_path": False}, {"create_host_path": True}])
def test_compose_mount_policy_handles_omitted_false_and_rejects_host_path_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind: dict[str, bool]
) -> None:
    """Keep serialization compatibility without accepting automatic host-path creation."""

    services = {
        "api": {
            "environment": {},
            "ports": [{"host_ip": "127.0.0.1"}],
            "volumes": [
                {
                    "source": str(REPOSITORY_ROOT / "config" / "rag.toml"),
                    "target": "/config/rag.toml",
                    "read_only": True,
                    "bind": bind,
                }
            ],
        },
        "web": {"ports": [{"host_ip": "127.0.0.1"}]},
    }
    monkeypatch.setattr(
        "tests.integration.test_live_compose_configuration.render_configuration",
        lambda *_args, **_kwargs: {"services": services},
    )
    if bind.get("create_host_path", False):
        with pytest.raises(AssertionError):
            test_compose_live_defaults_to_citable_units_and_keeps_profile_api_only(
                tmp_path, live=False, profile="m1-5-citable-units", expected=None
            )
    else:
        test_compose_live_defaults_to_citable_units_and_keeps_profile_api_only(
            tmp_path, live=False, profile="m1-5-citable-units", expected=None
        )
