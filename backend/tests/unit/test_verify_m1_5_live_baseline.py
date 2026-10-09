"""Evaluation orchestration tests with no environment-file access, Docker, or providers."""

import json
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

from investment_research.modules.evidence.domain import ValidationRule
from investment_research.modules.operations.domain import ProviderOperationKind
from investment_research.settings import (
    ApiSettings,
    DenseProviderSettings,
    EmbeddingProviderSettings,
    GenerationProviderSettings,
    PineconeProviderSettings,
    WorkerSettings,
)
from scripts import m1_5_live_runner as runner
from scripts import m1_5_live_runtime as runtime
from scripts import verify_m1_5_live_baseline as cli
from scripts.m1_5_live_diagnostics import ProviderFailureDiagnostic
from scripts.m1_5_live_index import LiveIndexCleanupError
from scripts.m1_5_live_providers import LiveProviderCalls, ProviderCallRecord
from scripts.m1_5_live_runner import LiveBaselineReport, LiveCaseFailure, summarize_usage
from scripts.m1_5_live_settings import EvaluationConfigurationError
from scripts.m1_5_local_output import LocalEvaluationOutput
from scripts.plan_m1_5_live_baseline import (
    LiveAnswerPolicy,
    LiveBaselineBudget,
    LiveBaselineLimitError,
    LiveBaselinePlan,
    LiveSupportPolicy,
    build_plan,
)
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
    assert '"maximum_spend_micro_usd": 800000' in output
    assert MARKER not in output


def test_rrf_preflight_records_the_selected_policy_without_runtime_access(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--retrieval-policy", "rrf"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["configuration"]["selector_config_version"] == "m1-5-rrf-k60-v1"
    assert plan["maximum_spend_micro_usd"] == 800_000


def test_unknown_policy_is_rejected_before_runtime_access() -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["--retrieval-policy", "unknown"])
    assert caught.value.code == 2


def test_citable_preflight_is_explicit_free_and_keeps_budget(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--answer-policy", "citable_units", "--retrieval-policy", "rrf"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert (
        plan["configuration"]["answer_prompt_version"] == "m1-5-citable-units-en-answer-prompt-v5"
    )
    assert plan["configuration"]["selector_config_version"] == "m1-5-rrf-k60-v1"
    assert plan["maximum_spend_micro_usd"] == 800_000
    with pytest.raises(SystemExit):
        cli.main(["--answer-policy", "unknown"])


def test_command_rejects_budget_above_approved_cap_before_runtime_access() -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["--max-spend-usd", "0.81"])
    assert caught.value.code == 2


@pytest.mark.parametrize("answer_policy", ["m1", "citable_units"])
@pytest.mark.parametrize("show_output", [False, True])
@pytest.mark.parametrize("support_policy", ["m1", "attribution", "document_attribution"])
def test_runner_accepts_matching_inspection_plan_before_runtime_access(
    monkeypatch: pytest.MonkeyPatch,
    answer_policy: LiveAnswerPolicy,
    show_output: bool,
    support_policy: LiveSupportPolicy,
) -> None:
    class PlanAcceptedError(Exception):
        """Stop after real runner admission, before fixtures or resources are accessed."""

    def stop_after_admission(*args: object) -> None:
        raise PlanAcceptedError

    monkeypatch.setattr(runner, "validate_corpus", stop_after_admission)
    local = None
    if show_output:
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        local = LocalEvaluationOutput(
            answer_model="synthetic-answer", support_model="synthetic-support"
        )
    plan = build_plan(
        retrieval_policy="rrf",
        answer_policy=answer_policy,
        support_explanations=show_output,
        support_policy=support_policy,
    )
    with pytest.raises(PlanAcceptedError):
        runner.run_live_baseline(
            MagicMock(),
            plan,
            index=MagicMock(),
            document_embedding=MagicMock(),
            query_embedding=MagicMock(),
            generator=MagicMock(),
            support=MagicMock(),
            calls=LiveProviderCalls(LiveBaselineBudget(plan)),
            local_output=local,
        )


@pytest.mark.parametrize("mismatch", ["inspection", "budget", "prompt", "vector_limit"])
def test_runner_rejects_inconsistent_plans_before_runtime_access(
    monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    def forbidden(*args: object) -> None:
        pytest.fail("inconsistent runner plan must not access the corpus or runtime")

    monkeypatch.setattr(runner, "validate_corpus", forbidden)
    plan = build_plan(support_explanations=mismatch == "inspection")
    budget_plan = plan
    if mismatch == "budget":
        budget_plan = build_plan(maximum_spend_micro_usd=200_000)
    elif mismatch == "prompt":
        plan = plan.model_copy(
            update={
                "configuration": plan.configuration.model_copy(
                    update={"support_prompt_version": "unapproved-support-prompt"}
                )
            }
        )
        budget_plan = plan
    elif mismatch == "vector_limit":
        plan = plan.model_copy(
            update={"maximum_document_vectors": plan.maximum_document_vectors + 1}
        )
        budget_plan = plan
    with pytest.raises(LiveBaselineLimitError, match="plan is inconsistent"):
        runner.run_live_baseline(
            MagicMock(),
            plan,
            index=MagicMock(),
            document_embedding=MagicMock(),
            query_embedding=MagicMock(),
            generator=MagicMock(),
            support=MagicMock(),
            calls=LiveProviderCalls(LiveBaselineBudget(budget_plan)),
        )


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


@pytest.mark.parametrize("cleanup", [None, "pinecone", "database", "case", "local"])
def test_recorded_results_survive_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup: str | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "load_evaluation_settings", lambda: (MagicMock(), MagicMock()))

    @contextmanager
    def database() -> Iterator[Engine]:
        yield cast(Engine, MagicMock())
        if cleanup == "database":
            raise RuntimeError(MARKER)

    @contextmanager
    def resources(*args: object, **kwargs: object) -> Iterator[MagicMock]:
        assert (kwargs.get("local_output") is not None) == (cleanup == "local")
        actual_plan = cast(LiveBaselinePlan, args[2])
        assert (
            actual_plan.configuration.support_prompt_version
            == build_plan(
                support_explanations=cleanup == "local"
            ).configuration.support_prompt_version
        )
        yield MagicMock()
        if cleanup == "pinecone":
            steps = cast(list[str], args[-1])
            steps.append("namespace_absence")

    monkeypatch.setattr(cli, "isolated_database", database)
    monkeypatch.setattr(cli, "provider_resources", resources)
    measured = report()
    if cleanup == "case":
        measured = measured.model_copy(
            update={
                "failures": (
                    LiveCaseFailure(
                        case_id="synthetic-contract",
                        reason="provider",
                        stage="answer_generation_and_validation",
                        provider_failure=ProviderFailureDiagnostic(
                            category="contract",
                            role=ProviderOperationKind.ANSWER_GENERATION,
                            contract_detail="quote_not_found",
                        ),
                    ),
                    LiveCaseFailure(
                        case_id="synthetic-rejection",
                        reason="rejected",
                        stage="evidence_validation",
                        failed_validation_rules=(ValidationRule.STATEMENT_SUPPORT,),
                    ),
                )
            }
        )

    def measure(*args: object, **kwargs: object) -> LiveBaselineReport:
        local = kwargs.get("local_output")
        if cleanup == "local":
            assert isinstance(local, LocalEvaluationOutput)
            local.begin_case("synthetic-case", "Public synthetic question")
            local.answer_proposal("Public synthetic answer")
        else:
            assert local is None
        return measured

    monkeypatch.setattr(cli, "run_live_baseline", measure)
    if cleanup == "local":
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    output = tmp_path / "result.json"
    code = cli.main(
        [
            "--execute",
            "--confirm-public-synthetic",
            "--confirm-pinecone-starter",
            "--output",
            str(output),
            *(["--show-local-model-output"] if cleanup == "local" else []),
        ]
    )
    recorded = LiveBaselineReport.model_validate_json(output.read_bytes())
    assert code == (0 if cleanup in {None, "local"} else 1)
    assert recorded.cleanup_status == (
        "VERIFIED" if cleanup in {None, "case", "local"} else "FAILED"
    )
    assert len(recorded.observations.cases) == 6
    assert MARKER not in output.read_text(encoding="utf-8")
    terminal = capsys.readouterr().out
    assert MARKER not in terminal
    assert ("Public synthetic answer" in terminal) == (cleanup == "local")
    assert "Public synthetic answer" not in output.read_text(encoding="utf-8")
    assert "Public synthetic question" not in output.read_text(encoding="utf-8")
    if cleanup == "case":
        assert "stage=answer_generation_and_validation" in terminal
        assert "contract_detail=quote_not_found" in terminal
        assert "failed_rules=STATEMENT_SUPPORT" in terminal


@pytest.mark.parametrize("environment", ["CI", "GITHUB_ACTIONS", "redirected"])
def test_local_model_output_is_denied_before_runtime_access(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], environment: str
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: environment != "redirected")
    if environment != "redirected":
        monkeypatch.setenv(environment, "false")  # Presence is enough to deny output.
    assert cli.main(["--execute", "--show-local-model-output"]) == 2
    assert "interactive local terminal outside CI" in capsys.readouterr().err


def test_local_output_requires_explicit_execution() -> None:
    assert cli.main(["--show-local-model-output"]) == 2


def test_local_output_rechecks_environment_and_escapes_terminal_control_characters(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    local = LocalEvaluationOutput(
        answer_model="synthetic-answer", support_model="synthetic-support"
    )
    local.answer_proposal("Public answer\x1b[31m")
    terminal = capsys.readouterr().out
    assert "Unvalidated model output" in terminal
    assert "\x1b" not in terminal
    assert "\\u001b" in terminal
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(ValueError, match="outside CI"):
        local.answer_proposal("must not appear")
    assert "must not appear" not in capsys.readouterr().out


def test_older_reports_do_not_invent_failure_diagnostics() -> None:
    historical = report().model_dump(mode="json")
    historical["failures"] = [{"case_id": "synthetic-case", "reason": "provider"}]
    historical["provider_calls"] = [
        {
            "ordinal": 0,
            "role": "ANSWER_GENERATION",
            "usage_state": "UNKNOWN",
            "input_tokens": None,
            "output_tokens": None,
            "estimated_cost_micro_usd": None,
            "reserved_cost_micro_usd": 1,
        }
    ]
    loaded = LiveBaselineReport.model_validate_json(json.dumps(historical))
    assert loaded.failures[0].stage is None
    assert loaded.failures[0].provider_failure is None
    assert loaded.failures[0].failed_validation_rules == ()
    assert loaded.provider_calls[0].failure is None


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
@pytest.mark.parametrize("show_output", [False, True])
@pytest.mark.parametrize("answer_policy", ["m1", "citable_units"])
@pytest.mark.parametrize("support_policy", ["m1", "attribution", "document_attribution"])
def test_provider_clients_close_after_partial_setup_or_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
    show_output: bool,
    answer_policy: LiveAnswerPolicy,
    support_policy: LiveSupportPolicy,
) -> None:
    local = None
    if show_output:
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        local = LocalEvaluationOutput(
            answer_model="synthetic-answer", support_model="synthetic-support"
        )
    plan = build_plan(
        answer_policy=answer_policy, support_explanations=show_output, support_policy=support_policy
    )
    calls = LiveProviderCalls(LiveBaselineBudget(plan))
    worker, api = settings()
    document, query, answer, support, writer, retriever = (MagicMock() for _ in range(6))
    monkeypatch.setattr(
        runtime, "create_openai_embedding_provider", MagicMock(side_effect=[document, query])
    )
    answer_factory = MagicMock(return_value=answer)
    support_factory = MagicMock(return_value=support)
    legacy_factory = MagicMock(return_value=answer)
    citable_factory = MagicMock(return_value=answer)
    monkeypatch.setattr(runtime, "create_openai_answer_generator", legacy_factory)
    monkeypatch.setattr(runtime, "create_openai_citable_unit_answer_generator", citable_factory)
    answer_factory = citable_factory if answer_policy == "citable_units" else legacy_factory
    unused_factory = legacy_factory if answer_policy == "citable_units" else citable_factory
    monkeypatch.setattr(runtime, "create_openai_statement_support_evaluator", support_factory)
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
            runtime.provider_resources(worker, api, plan, calls, cleanup_steps, local_output=local),
        ):
            pytest.fail("partial setup must not yield resources")
        writer.close.assert_called_once()
    else:
        with runtime.provider_resources(
            worker, api, plan, calls, cleanup_steps, local_output=local
        ) as resources:
            assert resources.support.local_output is local
        owned_index.close.assert_called_once()
    for client in (document, query, answer, support):
        client.close.assert_called_once()
    unused_factory.assert_not_called()
    assert cleanup_steps == (["namespace_absence"] if failure == "cleanup" else [])
    assert answer_factory.call_args.kwargs["local_proposal_output"] == (
        None if local is None else local.answer_proposal
    )
    assert support_factory.call_args.kwargs["local_validation_output"] == (
        None if local is None else local.support_failure
    )
    assert support_factory.call_args.kwargs["include_decision_reason"] is show_output
    assert support_factory.call_args.kwargs["include_attribution_context"] is (
        support_policy != "m1"
    )
    assert support_factory.call_args.kwargs["include_document_attribution_context"] is (
        support_policy == "document_attribution"
    )


@pytest.mark.parametrize("show_output", [False, True])
@pytest.mark.parametrize("support_policy", ["m1", "attribution", "document_attribution"])
def test_support_inspection_prompt_mismatch_is_rejected_before_provider_setup(
    monkeypatch: pytest.MonkeyPatch,
    show_output: bool,
    support_policy: LiveSupportPolicy,
) -> None:
    local = None
    if show_output:
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        local = LocalEvaluationOutput(
            answer_model="synthetic-answer", support_model="synthetic-support"
        )
    factory = MagicMock(side_effect=AssertionError("provider must not be constructed"))
    monkeypatch.setattr(runtime, "create_openai_embedding_provider", factory)
    worker, api = settings()
    plan = build_plan(support_explanations=not show_output, support_policy=support_policy)
    with (
        pytest.raises(runtime.LiveRuntimeError, match="support prompt"),
        runtime.provider_resources(
            worker, api, plan, LiveProviderCalls(LiveBaselineBudget(plan)), [], local_output=local
        ),
    ):
        pytest.fail("mismatched inspection profile must not yield resources")
    factory.assert_not_called()


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
