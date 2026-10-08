"""Print the free preflight or explicitly execute a bounded public-synthetic M1.5 run."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from investment_research.modules.research.contracts import ResearchModel
from scripts.m1_5_live_providers import LiveProviderCalls
from scripts.m1_5_live_runner import LiveBaselineReport, run_live_baseline
from scripts.m1_5_live_runtime import isolated_database, provider_resources
from scripts.m1_5_live_settings import (
    EvaluationConfigurationError,
    load_evaluation_reranking_settings,
    load_evaluation_settings,
)
from scripts.m1_5_local_output import LocalEvaluationOutput, require_local_terminal
from scripts.plan_m1_5_live_baseline import (
    MAXIMUM_SPEND_MICRO_USD,
    LiveBaselineBudget,
    _budget_argument,
    build_plan,
)
from scripts.verify_m1_5_corpus import REPOSITORY_ROOT

OUTPUT_ROOT = REPOSITORY_ROOT / "outputs" / "m1_5"


def output_path(raw: Path) -> Path:
    """Allow new JSON reports only beneath the ignored evaluation output directory."""

    candidate = raw.resolve()
    if (
        not candidate.is_relative_to(OUTPUT_ROOT)
        or candidate.suffix != ".json"
        or candidate.exists()
    ):
        raise ValueError("use a new JSON report beneath outputs/m1_5")
    return candidate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retrieval-policy",
        choices=("m1", "rrf", "rrf_rerank"),
        default="m1",
        help="Select a frozen evaluation policy only; the application default remains M1.",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--support-policy",
        choices=("m1", "attribution", "document_attribution"),
        default="m1",
        help=(
            "Select exact-only, same-block, or same-document attribution; "
            "normal runtime remains M1."
        ),
    )
    parser.add_argument(
        "--answer-policy",
        choices=("m1", "citable_units"),
        default="m1",
        help="Select the opt-in source-unit contract; normal runtime defaults stay M1.",
    )
    parser.add_argument("--confirm-public-synthetic", action="store_true")
    parser.add_argument("--confirm-pinecone-starter", action="store_true")
    parser.add_argument(
        "--confirm-reranking-allowance",
        action="store_true",
        help="Confirm that the Starter account has allowance for up to six BGE requests.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--show-local-model-output",
        action="store_true",
        help="Inspect synthetic output and request short support reasons locally, never CI.",
    )
    for role in ("answer", "support"):
        parser.add_argument(
            f"--{role}-model",
            choices=tuple(model.value for model in ResearchModel),
            default=ResearchModel.TERRA.value,
        )
    parser.add_argument("--max-spend-usd", type=_budget_argument, default=MAXIMUM_SPEND_MICRO_USD)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.show_local_model_output:
        if not arguments.execute:
            print("Local model output requires --execute.", file=sys.stderr)
            return 2
        try:
            require_local_terminal()
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
    if arguments.execute and (
        not arguments.confirm_public_synthetic
        or not arguments.confirm_pinecone_starter
        or arguments.output is None
    ):
        print(
            "Execution requires both confirmations and a new --output JSON path.", file=sys.stderr
        )
        return 2
    stage = "preflight"
    if (
        arguments.execute
        and arguments.retrieval_policy == "rrf_rerank"
        and not arguments.confirm_reranking_allowance
    ):
        print("Reranking execution requires --confirm-reranking-allowance.", file=sys.stderr)
        return 2
    try:
        plan = build_plan(
            answer_model=ResearchModel(arguments.answer_model),
            support_model=ResearchModel(arguments.support_model),
            maximum_spend_micro_usd=arguments.max_spend_usd,
            retrieval_policy=arguments.retrieval_policy,
            answer_policy=arguments.answer_policy,
            support_explanations=arguments.show_local_model_output,
            support_policy=arguments.support_policy,
        )
        if not arguments.execute:
            print(plan.model_dump_json(indent=2))
            return 0
        local_output = (
            LocalEvaluationOutput(
                answer_model=plan.configuration.answer_model,
                support_model=plan.configuration.support_model,
            )
            if arguments.show_local_model_output
            else None
        )
        stage = "output"
        path = output_path(arguments.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Reserve the output exclusively before any provider setup or paid call.
        with path.open("x", encoding="utf-8") as output:
            stage = "configuration"
            worker, api = load_evaluation_settings()
            reranking_settings = (
                load_evaluation_reranking_settings(
                    monthly_request_limit=plan.reranking_limit.maximum_requests,
                    starter_allowance_confirmed=arguments.confirm_reranking_allowance,
                    public_synthetic_content_approved=arguments.confirm_public_synthetic,
                )
                if plan.reranking_limit is not None
                else None
            )
            calls = LiveProviderCalls(LiveBaselineBudget(plan))
            cleanup_steps: list[str] = []
            stage = "runtime"
            report: LiveBaselineReport | None = None
            try:
                with isolated_database() as engine:
                    with provider_resources(
                        worker,
                        api,
                        plan,
                        calls,
                        cleanup_steps,
                        local_output=local_output,
                        reranking_settings=reranking_settings,
                    ) as resources:
                        stage = "evaluation"
                        report = run_live_baseline(
                            engine,
                            plan,
                            index=resources.index,
                            document_embedding=resources.document_embedding,
                            query_embedding=resources.query_embedding,
                            generator=resources.generator,
                            support=resources.support,
                            calls=calls,
                            local_output=local_output,
                            reranker=resources.reranker,
                        )
                    stage = "database_cleanup"
            except Exception:
                if report is None:
                    raise
                cleanup_steps.append("database_cleanup")
            report = report.model_copy(
                update={
                    "cleanup_status": "FAILED" if cleanup_steps else "VERIFIED",
                    "cleanup_steps": tuple(cleanup_steps),
                }
            )
            stage = "output"
            output.write(report.model_dump_json(indent=2))
        print(
            "M1.5 live baseline recorded: "
            f"selector={plan.configuration.selector_config_version}, "
            f"cases={report.summary.case_count}, failures={len(report.failures)}, "
            f"reserved_micro_usd={report.reserved_cost_micro_usd}, "
            f"cleanup={report.cleanup_status.lower()}, independent_support_review=pending"
        )
        for failure in report.failures:
            fields = [
                f"case={failure.case_id}",
                f"stage={failure.stage or 'unknown'}",
                f"reason={failure.reason}",
            ]
            diagnostic = failure.provider_failure
            if diagnostic is not None:
                fields.append(f"provider_category={diagnostic.category}")
                if diagnostic.role is not None:
                    fields.append(f"role={diagnostic.role.value}")
                if diagnostic.http_status is not None:
                    fields.append(f"http_status={diagnostic.http_status}")
                if diagnostic.contract_detail is not None:
                    fields.append(f"contract_detail={diagnostic.contract_detail}")
            if failure.failed_validation_rules:
                fields.append(
                    "failed_rules="
                    + ",".join(rule.value for rule in failure.failed_validation_rules)
                )
            print("M1.5 case failed: " + ", ".join(fields))
        return 1 if report.failures or cleanup_steps else 0
    except EvaluationConfigurationError as error:
        print(f"M1.5 live baseline failed at stage={stage}; {error}.", file=sys.stderr)
        return 1
    except Exception:
        print(f"M1.5 live baseline failed at stage={stage}; details withheld.", file=sys.stderr)
        if stage == "output":
            print(
                "Use a new .json filename beneath outputs/m1_5; "
                "existing files are not overwritten.",
                file=sys.stderr,
            )
        return 1
    except KeyboardInterrupt:
        print("M1.5 live baseline interrupted; no acceptance claimed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
