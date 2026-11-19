"""Collect fresh research contexts and optional validated answers from an opted-in local app."""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from investment_research.bootstrap import ApiContainer, build_api_container
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    ResearchModel,
    ResearchRetrievalInput,
)
from investment_research.settings import ApiSettings, load_api_settings
from scripts.evaluation_profiles import (
    DEFAULT_PROFILE,
    EvaluationProfileError,
    ResolvedEvaluationProfile,
    checked_repository_file,
    resolve_profile,
)
from scripts.portfolio_discovery import collect_discovery, discovery_commands
from scripts.portfolio_live_runtime import (
    LiveEvaluationError,
    RunBudget,
    RunBudgetExceededError,
    budget_for,
    code_digest,
    composition_for,
    inventory,
    inventory_digest,
    sources_for,
    validate_live_profile,
)
from scripts.portfolio_snapshots import (
    EvaluationSnapshot,
    RecordedPassage,
    RecordedSelection,
    RecordedSource,
    score_selection,
    settings_for,
)
from scripts.score_portfolio_retrieval import Case


def preview(resolved: ResolvedEvaluationProfile) -> dict[str, object]:
    validate_live_profile(resolved)
    research = sum(case.kind != "discovery" for case in resolved.cases)
    return {
        "mode": resolved.profile.mode,
        "case_ids": [case.id for case in resolved.cases],
        "new_research_requests": research * resolved.profile.repetitions,
        "new_archive_searches": (len(resolved.cases) - research)
        * len(resolved.profile.archive.modes)
        * resolved.profile.repetitions,
        "generates_answers": resolved.profile.mode == "rag_live" and research > 0,
        "maximum_reserved_model_usd": resolved.profile.budget.max_spend_usd,
        "maximum_provider_calls": resolved.profile.budget.max_provider_calls,
        "settings": settings_for(resolved).model_dump(mode="json"),
        "archive_modes": resolved.profile.archive.modes,
        "provider_calls_made": 0,
        "limitations": [
            "Pinecone database query charges are excluded from the model USD estimate; "
            "calls are capped.",
            "The call budget includes embeddings, dense queries, planning, reranking "
            "and answer/check attempts.",
            "Conservative reservations remain consumed after errors and can stop a run early.",
            "Execution creates research records in the local app; "
            "it does not import or reindex documents.",
            "Only reviewed public/synthetic archives may be used; "
            "raw local results must stay ignored.",
        ],
    }


def selection(
    case: Case,
    repetition: int,
    prepared: AnswerGenerationInput,
    sources: tuple[RecordedSource, ...],
) -> RecordedSelection:
    by_document = {source.document_id: source for source in sources}
    passages = []
    for item in prepared.sources:
        source = by_document.get(item.document_id)
        if source is None or source.document_version_id != item.document_version_id:
            raise LiveEvaluationError("context is outside the frozen current corpus")
        passages.append(
            RecordedPassage(
                chunk_id=item.chunk_id, source=source.alias, page=item.page_number, text=item.text
            )
        )
    return RecordedSelection(
        case=case.id,
        question=case.question,
        repetition=repetition,
        stage="final_generation_context",
        selected=tuple(passages),
    )


def collect_case(
    container: ApiContainer,
    resolved: ResolvedEvaluationProfile,
    sources: tuple[RecordedSource, ...],
    case: Case,
    repetition: int,
) -> tuple[RecordedSelection | None, dict[str, object]]:
    profile = resolved.profile
    context = container.identity_context_provider.resolve()
    ids = {source.alias: source.document_id for source in sources}
    command = ResearchRetrievalInput(
        question=case.question,
        document_ids=(
            tuple(ids[alias] for alias in case.document_scope)
            if profile.scope == "catalog_documents"
            else ()
        ),
        generation_model=ResearchModel(profile.models.answer),
        support_model=ResearchModel(profile.models.evidence_check),
        decompose_query=profile.retrieval.decompose_query,
        rerank_with_luna=profile.reranking.strategy == "luna",
    )
    selected: RecordedSelection | None = None
    record: dict[str, object] = {"case": case.id, "repetition": repetition}

    def capture(prepared: AnswerGenerationInput) -> None:
        nonlocal selected
        selected = selection(case, repetition, prepared, sources)

    started = monotonic()
    try:
        if profile.mode == "retrieval_live":
            retrieval = container.retrieve_research_service.retrieve(context, command)
            services = container.answer_research_request_service.model_services
            if services is None:
                raise LiveEvaluationError("requested live model pair is unavailable")
            service = services[(command.generation_model, command.support_model)]
            capture(service.prepare_generation_input(context, retrieval))
            record["status"] = "retrieved"
        else:
            result = container.answer_research_request_service.answer(
                context,
                command,
                on_context=capture,
                on_rejection=lambda proposal, decision: record.update(
                    rejected_proposal=proposal.model_dump(mode="json"),
                    rejection=decision.model_dump(mode="json"),
                ),
            )
            record.update(status=result.answer.state.value, result=result.model_dump(mode="json"))
    except RunBudgetExceededError:
        raise
    except Exception as error:
        # Never print provider response bodies, connection URLs, credentials or exception messages.
        record.update(status="failed", exception_type=type(error).__name__)
    record["elapsed_ms"] = round((monotonic() - started) * 1_000)
    if selected is not None:
        record["retrieval_score"] = score_selection(case, selected)
    record["manual_answer_review_required"] = True
    return selected, record


def collect(
    container: ApiContainer, resolved: ResolvedEvaluationProfile, budget: RunBudget, directory: Path
) -> dict[str, object]:
    """Journal each result and retain partial failures; never turn markers into answer verdicts."""

    context = container.identity_context_provider.resolve()
    initial = inventory(container, context)
    initial_digest = inventory_digest(initial)
    sources = sources_for(resolved, initial)
    commands = discovery_commands(container, context, resolved)
    rows: list[RecordedSelection] = []
    observations = 0
    failed_observations = 0
    manifest: dict[str, object] = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "plan": preview(resolved),
        "profile_sha256": hashlib.sha256(resolved.profile_bytes).hexdigest(),
        "catalog_sha256": hashlib.sha256(resolved.catalog_bytes).hexdigest(),
        "rag_config_sha256": hashlib.sha256(resolved.rag_bytes).hexdigest(),
        "code_sha256": code_digest(),
        "inventory_sha256": initial_digest,
        "status": "incomplete",
        "sources": [source.model_dump(mode="json") for source in sources],
        "source_original_hashes_verified": False,
        "observations": 0,
        "failed_observations": 0,
    }
    try:
        with (directory / "results.jsonl").open("x", encoding="utf-8") as journal:
            for repetition in range(1, resolved.profile.repetitions + 1):
                for case in resolved.cases:
                    if inventory_digest(inventory(container, context)) != initial_digest:
                        raise LiveEvaluationError("canonical inventory changed during execution")
                    if commands != discovery_commands(container, context, resolved):
                        raise LiveEvaluationError(
                            "discovery company bindings changed during execution"
                        )
                    modes = resolved.profile.archive.modes if case.kind == "discovery" else (None,)
                    for mode in modes:
                        row, record = (
                            collect_discovery(
                                container, sources, case, repetition, commands[(case.id, mode)]
                            )
                            if mode is not None
                            else collect_case(container, resolved, sources, case, repetition)
                        )
                        journal.write(json.dumps(record, ensure_ascii=False) + "\n")
                        journal.flush()
                        observations += 1
                        if record["status"] == "failed":
                            failed_observations += 1
                        if row is not None:
                            rows.append(row)
                        if budget.exhausted:
                            raise RunBudgetExceededError("evaluation run budget exhausted")
            if inventory_digest(inventory(container, context)) != initial_digest:
                raise LiveEvaluationError("canonical inventory changed during execution")
            if commands != discovery_commands(container, context, resolved):
                raise LiveEvaluationError("discovery company bindings changed during execution")
            manifest["status"] = "completed"
    except RunBudgetExceededError:
        manifest["status"] = "budget_exhausted"
    except Exception as error:
        manifest.update(status="failed", exception_type=type(error).__name__)
    finally:
        manifest.update(
            observations=observations,
            failed_observations=failed_observations,
            provider_attempts=budget.attempts,
            provider_calls=budget.calls,
            reserved_model_cost_micro_usd=budget.reserved_micro_usd,
            finished_at=datetime.now(UTC).isoformat(),
        )
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if rows:
            snapshot = EvaluationSnapshot(
                schema_version=1,
                catalog_sha256=hashlib.sha256(resolved.catalog_bytes).hexdigest(),
                code_sha256=str(manifest["code_sha256"]),
                recorded_versions={
                    "collector": "portfolio-live-v1",
                    **{
                        f"operation-{index}": str(item["config"])
                        for index, item in enumerate(budget.attempts)
                        if "config" in item
                    },
                },
                settings=settings_for(resolved),
                sources=sources,
                selections=tuple(rows),
            )
            (directory / "snapshot.json").write_text(snapshot.model_dump_json(indent=2) + "\n")
    return manifest


def execute(resolved: ResolvedEvaluationProfile, output: str) -> dict[str, object]:
    validate_live_profile(resolved)
    destination = checked_repository_file(
        output + "/manifest.json", subtree="outputs", suffixes=(".json",)
    ).parent
    if destination.name == "outputs":
        raise LiveEvaluationError("choose a new run directory beneath outputs")
    if destination.exists():
        raise LiveEvaluationError("output directory already exists; choose a new run name")
    # Runtime loading is application-owned; never include settings/secrets in any output.
    settings = load_api_settings(env_file=None)
    needs_providers = any(case.kind != "discovery" for case in resolved.cases) or any(
        mode != "keyword" for mode in resolved.profile.archive.modes
    )
    if (
        settings.environment != "development"
        or settings.research_profile != "m1-5-citable-units"
        or (needs_providers and settings.query_providers is None)
    ):
        raise LiveEvaluationError("use the local development M1.5 API runtime with live providers")
    settings = ApiSettings.model_validate(
        settings.model_dump() | {"rag": resolved.rag, "local_model_failure_output": False}
    )
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    (destination / "profile.toml").write_bytes(resolved.profile_bytes)
    (destination / "catalog.json").write_bytes(resolved.catalog_bytes)
    (destination / "resolved.json").write_text(json.dumps(preview(resolved), indent=2) + "\n")
    budget = budget_for(resolved)
    container = None
    try:
        container = build_api_container(settings, options=composition_for(resolved, budget))
        return collect(container, resolved, budget, destination)
    except Exception as error:
        if not (destination / "manifest.json").exists():
            (destination / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "setup_failed",
                        "exception_type": type(error).__name__,
                        "provider_calls": budget.calls,
                        "reserved_model_cost_micro_usd": budget.reserved_micro_usd,
                    },
                    indent=2,
                )
                + "\n"
            )
        raise
    finally:
        if container is not None:
            container.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-public-synthetic", action="store_true")
    parser.add_argument("--output", help="New ignored run directory beneath outputs")
    args = parser.parse_args()
    try:
        resolved = resolve_profile(args.profile, case_ids=tuple(args.case))
        plan = preview(resolved)
        if not args.execute:
            print(json.dumps(plan, indent=2))
            return 0
        if not args.confirm_public_synthetic or not args.output:
            raise LiveEvaluationError(
                "execution requires public/synthetic confirmation and --output"
            )
        result = execute(resolved, args.output)
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in (
                        "status",
                        "observations",
                        "failed_observations",
                        "provider_calls",
                        "reserved_model_cost_micro_usd",
                    )
                }
            )
        )
        return 0 if result["status"] == "completed" and result["failed_observations"] == 0 else 1
    except Exception as error:
        print(f"Evaluation stopped: {type(error).__name__}; details withheld.", file=sys.stderr)
        if isinstance(error, (LiveEvaluationError, EvaluationProfileError)):
            print(str(error), file=sys.stderr)
        if isinstance(error, (LiveEvaluationError, EvaluationProfileError, FileExistsError)):
            print(
                "Check the profile, supported research cases, runtime and unused output directory.",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
