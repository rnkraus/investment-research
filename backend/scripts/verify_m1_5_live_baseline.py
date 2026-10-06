"""Print the free preflight or explicitly execute a bounded public-synthetic M1.5 run."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from investment_research.modules.research.contracts import ResearchModel
from investment_research.settings import load_api_settings, load_worker_settings
from scripts.m1_5_live_providers import LiveProviderCalls
from scripts.m1_5_live_runner import LiveBaselineReport, run_live_baseline
from scripts.m1_5_live_runtime import isolated_database, provider_resources
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
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-public-synthetic", action="store_true")
    parser.add_argument("--confirm-pinecone-starter", action="store_true")
    parser.add_argument("--output", type=Path)
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
    try:
        plan = build_plan(
            answer_model=ResearchModel(arguments.answer_model),
            support_model=ResearchModel(arguments.support_model),
            maximum_spend_micro_usd=arguments.max_spend_usd,
        )
        if not arguments.execute:
            print(plan.model_dump_json(indent=2))
            return 0
        stage = "output"
        path = output_path(arguments.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Reserve the output exclusively before any provider setup or paid call.
        with path.open("x", encoding="utf-8") as output:
            stage = "configuration"
            worker, api = load_worker_settings(), load_api_settings()
            calls = LiveProviderCalls(LiveBaselineBudget(plan))
            cleanup_steps: list[str] = []
            stage = "runtime"
            report: LiveBaselineReport | None = None
            try:
                with isolated_database() as engine:
                    with provider_resources(worker, api, plan, calls, cleanup_steps) as resources:
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
            f"cases={report.summary.case_count}, failures={len(report.failures)}, "
            f"reserved_micro_usd={report.reserved_cost_micro_usd}, "
            f"cleanup={report.cleanup_status.lower()}, independent_support_review=pending"
        )
        return 1 if report.failures or cleanup_steps else 0
    except Exception:
        print(f"M1.5 live baseline failed at stage={stage}; details withheld.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("M1.5 live baseline interrupted; no acceptance claimed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
