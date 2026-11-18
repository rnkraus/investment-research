"""Print a validated evaluation preview without environment loading, I/O writes, or execution."""

import argparse
import hashlib
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from investment_research.infrastructure.evidence.openai import (
    M1_5_DOCUMENT_ATTRIBUTION_BATCH_VERSION,
    statement_support_prompt_version,
)
from investment_research.infrastructure.generation.openai_citable_units import (
    citable_answer_profile,
)
from investment_research.infrastructure.search.openai import OPENAI_EMBEDDING_MODEL
from investment_research.modules.processing.application import M1_PROCESSING_PROFILE
from investment_research.modules.processing.contracts import M1_MAX_BLOCK_CHARACTERS
from investment_research.modules.research.citable_unit_contracts import (
    M1_5_CITABLE_UNIT_POLICY_VERSION,
)
from investment_research.modules.research.contracts import ResearchModel
from investment_research.modules.search.contracts import M1_EMBEDDING_DIMENSIONS
from investment_research.modules.search.fusion_contracts import (
    COMPANY_RRF_RANK_BONUS,
    RRF_CONFIG_VERSION,
    RRF_RANK_CONSTANT,
)
from investment_research.modules.search.page_context import PAGE_CONTEXT_SLOTS
from investment_research.modules.search.text_neighbors import (
    TEXT_NEIGHBOR_ANCHOR_LIMIT,
    TEXT_NEIGHBOR_RADIUS,
)
from investment_research.rag_settings import RagConfigurationError, load_rag_settings
from scripts.evaluation_profiles import (
    DEFAULT_PROFILE,
    REPOSITORY_ROOT,
    EvaluationProfileError,
    checked_repository_file,
    effective_rag_settings,
    load_profile,
)
from scripts.score_portfolio_retrieval import Catalog, SnapshotError, validate_catalog


def build_preview(path: Path = DEFAULT_PROFILE) -> dict[str, object]:
    """Resolve the experiment and state future prerequisites, never invoke its requested mode."""

    profile, profile_bytes = load_profile(path)
    catalog_path = checked_repository_file(profile.catalog, subtree="eval", suffixes=(".json",))
    catalog_bytes = catalog_path.read_bytes()
    catalog = Catalog.model_validate_json(catalog_bytes)
    validate_catalog(catalog, require_portfolio_cases=False)
    by_id = {case.id: case for case in catalog.cases}
    mentioned = set(profile.cases.include) | set(profile.cases.exclude)
    if not mentioned <= by_id.keys():
        raise EvaluationProfileError("case selection contains IDs absent from the catalog")
    wanted = set(profile.cases.include) if profile.cases.include else set(by_id)
    selected = [case for case in catalog.cases if case.id in wanted - set(profile.cases.exclude)]
    if not selected:
        raise EvaluationProfileError("case selection is empty")
    rag_path = REPOSITORY_ROOT / "config/rag.toml"
    rag = effective_rag_settings(profile, load_rag_settings(rag_path))
    counts = Counter(case.kind for case in selected)
    discovery_requests = counts["discovery"] * len(profile.archive.modes) * profile.repetitions
    research_requests = (counts["answer"] + counts["abstention"]) * profile.repetitions
    possible_roles: set[str] = set()
    requirements: list[str] = []
    if profile.mode == "offline_replay":
        requirements.append(
            "Compatible saved inputs must prove case, scope, and configuration identity."
        )
        if not profile.inputs.snapshot:
            requirements.append("Choose an ignored snapshot before replay execution.")
        if profile.retrieval.text_neighbors or profile.retrieval.page_expansion:
            requirements.append(
                "Expansion requires saved eligible neighbor/page candidates, not only final hits."
            )
        if profile.retrieval.decompose_query or profile.reranking.strategy != "none":
            requirements.append(
                "Reuse compatible recorded planning/reranking outputs; "
                "never call a provider offline."
            )
    elif not profile.inputs.snapshot:
        if research_requests or (
            discovery_requests and any(mode != "keyword" for mode in profile.archive.modes)
        ):
            possible_roles.update(("query_embedding", "pinecone_query"))
        if research_requests:
            if profile.retrieval.decompose_query:
                possible_roles.add("query_planning")
            if profile.reranking.strategy != "none":
                possible_roles.add("reranking")
    if profile.mode == "rag_live" and research_requests:
        possible_roles.update(("answer_generation", "statement_support"))
    if profile.inputs.snapshot and profile.mode == "rag_live":
        requirements.append(
            "Reusing saved generation contexts requires canonical requalification "
            "and matching questions/configurations."
        )
    if profile.mode != "offline_replay":
        if possible_roles and (
            Decimal(profile.budget.max_spend_usd) <= 0 or profile.budget.max_provider_calls <= 0
        ):
            raise EvaluationProfileError(
                "provider-backed execution requires positive spend and call limits"
            )
        requirements.extend(
            (
                "Live execution needs explicit authorization, reviewed public/synthetic sources, "
                "and run/workspace budgets.",
                "Verify current canonical versions and complete index state "
                "before collecting live results.",
            )
        )
    answer_profile = citable_answer_profile(ResearchModel(profile.models.answer))
    fixed_settings = {
        "embedding_model": OPENAI_EMBEDDING_MODEL,
        "embedding_dimensions": M1_EMBEDDING_DIMENSIONS,
        "embedding_config_version": M1_PROCESSING_PROFILE.embedding_config_version,
        "dense_index_config_version": M1_PROCESSING_PROFILE.dense_index_config_version,
        "lexical_config_version": M1_PROCESSING_PROFILE.lexical_config_version,
        "parser_version": M1_PROCESSING_PROFILE.parser_version,
        "parser_config_version": M1_PROCESSING_PROFILE.parser_config_version,
        "chunker_version": M1_PROCESSING_PROFILE.chunker_version,
        "chunking_config_version": M1_PROCESSING_PROFILE.chunking_config_version,
        "maximum_block_characters": M1_MAX_BLOCK_CHARACTERS,
        "source_unit_policy_version": M1_5_CITABLE_UNIT_POLICY_VERSION,
        "rrf_version": RRF_CONFIG_VERSION,
        "rrf_k": RRF_RANK_CONSTANT,
        "company_rank_bonus": COMPANY_RRF_RANK_BONUS,
        "page_context_slots": PAGE_CONTEXT_SLOTS,
        "neighbor_radius": TEXT_NEIGHBOR_RADIUS,
        "neighbor_anchor_limit": TEXT_NEIGHBOR_ANCHOR_LIMIT,
        "answer_profile": answer_profile.model_dump(mode="json"),
        "support_prompt_version": statement_support_prompt_version(
            attribution=True, explanations=True, document_attribution=True
        ),
        "support_batch_version": M1_5_DOCUMENT_ATTRIBUTION_BATCH_VERSION,
    }
    return {
        "schema_version": 1,
        "execution_mode": "preflight_only",
        "execution_available": False,
        "provider_calls_made": 0,
        "name": profile.name,
        "requested_mode": profile.mode,
        "profile_sha256": hashlib.sha256(profile_bytes).hexdigest(),
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "rag_config_sha256": hashlib.sha256(rag_path.read_bytes()).hexdigest(),
        "case_ids": [case.id for case in selected],
        "case_counts": {kind: counts[kind] for kind in ("discovery", "answer", "abstention")},
        "planned_discovery_evaluations": discovery_requests,
        "planned_research_evaluations": research_requests,
        "research_generates_answers": profile.mode == "rag_live" and research_requests > 0,
        "possible_live_provider_roles": sorted(possible_roles),
        "profile_settings": profile.model_dump(mode="json"),
        "effective_rag_settings": rag.model_dump(mode="json"),
        "fixed_settings": fixed_settings,
        "discovery_scope": "Retain each selected discovery case's reviewed catalog filters.",
        "requirements_before_execution": requirements,
        "limitations": [
            "This command validates and previews only; it does not execute an evaluation.",
            "Snapshot compatibility, code/source identities, provider availability, "
            "and hard run-budget enforcement are not verified by this preview.",
            "Provider roles are possibilities, not request counts or a price estimate.",
            "No verdict or quality score is produced. "
            "Archive discovery and research remain separate measurements.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    arguments = parser.parse_args()
    try:
        preview = build_preview(arguments.profile)
    except EvaluationProfileError as error:
        print(f"Evaluation preview rejected: {error}", file=sys.stderr)
        return 1
    except OSError, UnicodeError, ValidationError, SnapshotError, RagConfigurationError:
        print(
            "Evaluation preview rejected: invalid catalog or shared RAG configuration",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(preview, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
