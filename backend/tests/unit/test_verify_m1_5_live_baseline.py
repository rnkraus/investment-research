"""Evaluation orchestration tests with no environment-file access, Docker, or providers."""

import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

from investment_research.modules.operations.domain import ProviderOperationKind
from investment_research.settings import (
    ApiSettings,
    DenseProviderSettings,
    EmbeddingProviderSettings,
    GenerationProviderSettings,
    PineconeProviderSettings,
    WorkerSettings,
)
from scripts import m1_5_live_runtime as runtime
from scripts import verify_m1_5_live_baseline as cli
from scripts.m1_5_live_index import LiveIndexCleanupError
from scripts.m1_5_live_providers import LiveProviderCalls, ProviderCallRecord
from scripts.m1_5_live_runner import LiveBaselineReport, summarize_usage
from scripts.m1_5_live_settings import EvaluationConfigurationError
from scripts.plan_m1_5_live_baseline import LiveBaselineBudget, build_plan
from scripts.score_m1_5_observations import ProviderUsage
from tests.unit.test_score_m1_5_observations import (
    make_complete_observations,
    score_controlled_observations,
)

MARKER = "synthetic-private-error-marker"


def report() -> LiveBaselineReport:
    plan = build_plan()
    base = make_complete_observations()
    observations = base.model_copy(
        update={
            "configuration": plan.configuration,
            "run_mode": "live_opt_in",
            "cases": tuple(
                case.model_copy(
                    update={
                        "provider_usage": ProviderUsage(
                            input_tokens=0, output_tokens=0, estimated_cost_micro_usd=0
                        ),
                    }
                )
                for case in base.cases
            ),
        }
    )
    usage = ProviderUsage(input_tokens=0, output_tokens=0, estimated_cost_micro_usd=0)
    return LiveBaselineReport(
        admission_plan=plan,
        observations=observations,
        summary=score_controlled_observations(observations),
        failures=(),
        preparation_failed=False,
        preparation_elapsed_ms=1,
        provider_calls=(),
        preparation_usage=usage,
        total_usage=usage,
        reserved_cost_micro_usd=0,
    )


@pytest.fixture(autouse=True)
def reject_runtime_access_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("unit test attempted runtime access")

    for name in (
        "load_evaluation_settings",
        "isolated_database",
        "provider_resources",
    ):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


def test_default_command_is_free_and_content_free(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert '"preflight_only"' in output
    assert '"maximum_spend_micro_usd": 300000' in output
    assert MARKER not in output


def test_command_rejects_budget_above_approved_cap_before_runtime_access() -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["--max-spend-usd", "0.31"])
    assert caught.value.code == 2


def test_rejected_usage_is_still_measured_but_missing_usage_is_not_zero() -> None:
    rejected = ProviderCallRecord(
        ordinal=0,
        role=ProviderOperationKind.ANSWER_GENERATION,
        usage_state="USAGE_REJECTED",
        input_tokens=10,
        output_tokens=2_001,
        estimated_cost_micro_usd=24_032,
        reserved_cost_micro_usd=24_000,
    )
    unknown = ProviderCallRecord(
        ordinal=1,
        role=ProviderOperationKind.ANSWER_GENERATION,
        usage_state="UNKNOWN",
        input_tokens=None,
        output_tokens=None,
        estimated_cost_micro_usd=None,
        reserved_cost_micro_usd=24_000,
    )
    assert summarize_usage((rejected,)).output_tokens == 2_001
    assert summarize_usage((rejected,)).estimated_cost_micro_usd == 24_032
    assert summarize_usage((rejected, unknown)).estimated_cost_micro_usd is None
    assert summarize_usage(()).estimated_cost_micro_usd == 0


def test_subprocess_failure_withholds_captured_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = MagicMock(
        return_value=subprocess.CompletedProcess(
            ["docker", "compose", "ps"],
            1,
            stdout=MARKER.encode(),
            stderr=MARKER.encode(),
        )
    )
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(runtime.LiveRuntimeError) as caught:
        runtime._run(["docker", "compose", "ps"], {})
    assert MARKER not in str(caught.value)
    assert MARKER not in capsys.readouterr().out
    assert run.call_args.kwargs["capture_output"] is True


@pytest.mark.parametrize(
    "flags",
    [
        ["--execute"],
        ["--execute", "--confirm-public-synthetic"],
        ["--execute", "--confirm-pinecone-starter"],
        ["--execute", "--confirm-public-synthetic", "--confirm-pinecone-starter"],
    ],
)
def test_missing_consent_or_output_never_loads_keys(flags: list[str]) -> None:
    assert cli.main(flags) == 2


def test_output_path_rejects_existing_outside_and_symlink_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "reports"
    root.mkdir()
    monkeypatch.setattr(cli, "OUTPUT_ROOT", root)
    assert cli.output_path(root / "new.json") == root / "new.json"
    existing = root / "existing.json"
    existing.write_text("retained", encoding="utf-8")
    for path in (existing, tmp_path / "outside.json", root / "wrong.txt"):
        with pytest.raises(ValueError):
            cli.output_path(path)
    escape = root / "escape"
    escape.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        cli.output_path(escape / "outside.json")
    assert existing.read_text(encoding="utf-8") == "retained"


@pytest.mark.parametrize("cleanup", [None, "pinecone", "database"])
def test_recorded_results_survive_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup: str | None,
) -> None:
    monkeypatch.setattr(cli, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "load_evaluation_settings", lambda: (MagicMock(), MagicMock()))

    @contextmanager
    def database() -> Iterator[Engine]:
        yield cast(Engine, MagicMock())
        if cleanup == "database":
            raise RuntimeError(MARKER)

    @contextmanager
    def resources(*args: object) -> Iterator[MagicMock]:
        yield MagicMock()
        if cleanup == "pinecone":
            steps = cast(list[str], args[-1])
            steps.append("namespace_absence")

    monkeypatch.setattr(cli, "isolated_database", database)
    monkeypatch.setattr(cli, "provider_resources", resources)
    monkeypatch.setattr(cli, "run_live_baseline", lambda *args, **kwargs: report())
    output = tmp_path / "result.json"
    code = cli.main(
        [
            "--execute",
            "--confirm-public-synthetic",
            "--confirm-pinecone-starter",
            "--output",
            str(output),
        ]
    )
    recorded = LiveBaselineReport.model_validate_json(output.read_bytes())
    assert code == (0 if cleanup is None else 1)
    assert recorded.cleanup_status == ("VERIFIED" if cleanup is None else "FAILED")
    assert len(recorded.observations.cases) == 6
    assert MARKER not in output.read_text(encoding="utf-8")


def test_configuration_failure_names_only_safe_setting_and_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "OUTPUT_ROOT", tmp_path)

    def fail() -> None:
        raise EvaluationConfigurationError(
            "INVESTMENT_RESEARCH_OPENAI_DOCUMENT_API_KEY", "credential_missing_or_not_normalized"
        )

    monkeypatch.setattr(cli, "load_evaluation_settings", fail)
    code = cli.main(
        [
            "--execute",
            "--confirm-public-synthetic",
            "--confirm-pinecone-starter",
            "--output",
            str(tmp_path / "result.json"),
        ]
    )
    assert code == 1
    error = capsys.readouterr().err
    assert "stage=configuration" in error
    assert "setting=INVESTMENT_RESEARCH_OPENAI_DOCUMENT_API_KEY" in error
    assert "reason=credential_missing_or_not_normalized" in error
    assert MARKER not in error


def test_existing_report_explains_new_filename_without_loading_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "OUTPUT_ROOT", tmp_path)
    output = tmp_path / "result.json"
    output.write_text("retained", encoding="utf-8")
    assert (
        cli.main(
            [
                "--execute",
                "--confirm-public-synthetic",
                "--confirm-pinecone-starter",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert "Use a new .json filename" in capsys.readouterr().err
    assert output.read_text(encoding="utf-8") == "retained"


def settings() -> tuple[WorkerSettings, ApiSettings]:
    providers = DenseProviderSettings(
        embedding=EmbeddingProviderSettings(api_key=SecretStr("synthetic-embedding")),
        pinecone=PineconeProviderSettings(
            api_key=SecretStr("synthetic-index"), index_host="example.pinecone.io"
        ),
    )
    common = {
        "environment": "test",
        "redis_url": "redis://:synthetic@127.0.0.1:1/0",
        "provider_monthly_budget_micro_usd": 5_000_000,
    }
    worker = WorkerSettings.model_validate(common | {"indexing_providers": providers})
    api = ApiSettings.model_validate(
        common
        | {
            "query_providers": providers,
            "generation_provider": GenerationProviderSettings(
                api_key=SecretStr("synthetic-answer")
            ),
        }
    )
    return worker, api


@pytest.mark.parametrize("failure", [None, "retriever_setup", "cleanup"])
def test_provider_clients_close_after_partial_setup_or_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    plan = build_plan()
    calls = LiveProviderCalls(LiveBaselineBudget(plan))
    worker, api = settings()
    document, query, answer, support, writer, retriever = (MagicMock() for _ in range(6))
    monkeypatch.setattr(
        runtime, "create_openai_embedding_provider", MagicMock(side_effect=[document, query])
    )
    monkeypatch.setattr(runtime, "create_openai_answer_generator", lambda **kwargs: answer)
    monkeypatch.setattr(
        runtime, "create_openai_statement_support_evaluator", lambda **kwargs: support
    )
    monkeypatch.setattr(runtime, "create_pinecone_dense_index_writer", lambda **kwargs: writer)
    factory = MagicMock(return_value=retriever)
    if failure == "retriever_setup":
        factory.side_effect = RuntimeError(MARKER)
    monkeypatch.setattr(runtime, "create_pinecone_dense_retriever", factory)
    owned_index = MagicMock()
    if failure == "cleanup":
        owned_index.close.side_effect = LiveIndexCleanupError(
            ("namespace_absence",), body_failed=False
        )
    monkeypatch.setattr(runtime, "IsolatedEvaluationIndex", MagicMock(return_value=owned_index))
    cleanup_steps: list[str] = []
    if failure == "retriever_setup":
        with (
            pytest.raises(RuntimeError),
            runtime.provider_resources(worker, api, plan, calls, cleanup_steps),
        ):
            pytest.fail("partial setup must not yield resources")
        writer.close.assert_called_once()
    else:
        with runtime.provider_resources(worker, api, plan, calls, cleanup_steps):
            pass
        owned_index.close.assert_called_once()
    for client in (document, query, answer, support):
        client.close.assert_called_once()
    assert cleanup_steps == (["namespace_absence"] if failure == "cleanup" else [])


@pytest.mark.parametrize("failure", [None, "startup", "migration", "body"])
def test_database_is_owned_migrated_and_always_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    monkeypatch.setattr(runtime, "find_docker_executable", lambda: "docker")
    monkeypatch.setattr(runtime, "find_available_loopback_port", lambda: 54321)
    monkeypatch.setattr(runtime, "generate_disposable_password", lambda: "synthetic-only")
    engine = MagicMock()
    monkeypatch.setattr(runtime, "create_engine", MagicMock(return_value=engine))
    commands: list[tuple[str, ...]] = []

    @contextmanager
    def fake_secrets(values: object) -> Iterator[dict[str, str]]:
        yield {}

    def command(args: Sequence[str], environment: object, *, cleanup: bool = False) -> None:
        commands.append(tuple(args))
        if (failure == "startup" and "up" in args) or (
            failure == "migration" and "alembic" in args
        ):
            raise RuntimeError(MARKER)

    monkeypatch.setattr(runtime, "disposable_compose_secret_environment", fake_secrets)
    monkeypatch.setattr(runtime, "_run", command)
    if failure:
        with pytest.raises(RuntimeError), runtime.isolated_database():
            if failure == "body":
                raise RuntimeError(MARKER)
    else:
        with runtime.isolated_database() as yielded:
            assert yielded is engine
    assert "down" in commands[-1]
    assert "--volumes" in commands[-1]
    assert commands[0][3].startswith("investment-research-m1-5-eval-")
    assert all("investment-research-m0-0" not in argument for cmd in commands for argument in cmd)
    if failure not in {"startup", "migration"}:
        engine.dispose.assert_called_once()
