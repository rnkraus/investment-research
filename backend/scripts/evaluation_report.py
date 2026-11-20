"""Render escaped, self-contained evaluation summaries without raw questions or source text."""

from collections.abc import Mapping
from html import escape
from pathlib import Path
from statistics import median

from scripts.evaluate_portfolio import compare_saved
from scripts.evaluation_profiles import checked_repository_file
from scripts.evaluation_run_artifacts import RunArtifactError, SavedRun
from scripts.portfolio_snapshots import evidence_ranks, selection_key

_STYLE = """
body{font:16px/1.5 system-ui,sans-serif;color:#17202e;background:#f5f7fa;margin:0}
main{max-width:1200px;margin:auto;padding:32px}h1,h2{line-height:1.2}
.notice,section{background:white;border:1px solid #dbe1e8;border-radius:12px;
padding:20px;margin:20px 0}
.notice{border-left:5px solid #60758c}.scroll{overflow:auto}
table{width:100%;border-collapse:collapse}th,td{text-align:left;vertical-align:top;
padding:12px;border-bottom:1px solid #dbe1e8}
th{background:#eef2f7}td{overflow-wrap:anywhere}.green{color:#14532d;background:#dcfce7}
.orange{color:#7c2d12;background:#ffedd5}.red{color:#7f1d1d;background:#fee2e2}
.gray{color:#334155;background:#e2e8f0}
.badge{display:inline-block;padding:3px 9px;border-radius:6px;font-weight:600}
code{font-size:.9em}dt{font-weight:600}dd{margin:0 0 12px}.small{font-size:.9em;color:#465568}
@media(max-width:700px){main{padding:16px}th,td{padding:8px}h1{font-size:1.7rem}}
"""


def text(value: object) -> str:
    return escape(str(value), quote=True)


def badge(label: str, color: str) -> str:
    return f'<span class="badge {color}">{text(label)}</span>'


def document(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        f"<title>{text(title)}</title><style>{_STYLE}</style></head><body><main>"
        f"<h1>{text(title)}</h1>"
        '<p class="notice">Colors measure recorded retrieval-marker coverage only. '
        "They do not establish correct answers, supported claims, citations or abstention. "
        "Archive discovery and answer quality require manual review. "
        "No provider calls were made to produce this report.</p>" + body + "</main></body></html>\n"
    )


def metadata(run: SavedRun, heading: str) -> str:
    rows = run.scored.rows if run.scored else ()
    cases = {case.id: case for case in run.definition.cases}
    answers = [row for row in rows if cases[row.case].kind == "answer"]
    complete = sum(all(evidence_ranks(cases[row.case], row).values()) for row in answers)
    timings = [item.elapsed_ms for item in run.observations]
    profile = run.definition.profile
    settings = run.manifest.plan.settings
    values: dict[str, object] = {
        "Run status": run.manifest.status,
        "Recorded / planned observations": f"{len(run.observations)} / {len(run.expected_keys)}",
        "Execution failures": run.manifest.failed_observations,
        "Answer contexts with all expected markers": f"{complete} / {len(answers)}",
        "Execution mode": profile.mode,
        "Research scope": settings.scope,
        "Answer / evidence-check model": (
            f"{profile.models.answer} / {profile.models.evidence_check}"
        ),
        "Lexical / reranking": f"{settings.lexical_strategy} / {settings.reranking}",
        "Neighbors / pages / company priority": (
            f"{settings.text_neighbors} / {settings.page_expansion} / {settings.company_priority}"
        ),
        "Query decomposition": settings.decompose_query,
        "Lexical / dense candidate limits": (
            f"{settings.rag.retrieval.lexical_candidate_limit} / "
            f"{settings.rag.retrieval.dense_candidate_limit}"
        ),
        "Generation input overhead limit": settings.rag.generation.input_overhead_limit,
        "Context limits": (
            f"{settings.rag.context.chunk_limit} chunks / "
            f"{settings.rag.context.character_limit} characters"
        ),
        "Provider invocations": run.manifest.provider_calls,
        "Model cost reservation (not billed cost)": (
            f"USD {run.manifest.reserved_model_cost_micro_usd / 1_000_000:.6f}"
        ),
        "Actual tokens and estimated usage cost": "Not captured by this runner version",
        "Median / sum of recorded request durations": (
            f"{median(timings):g} ms / {sum(timings)} ms" if timings else "Not recorded"
        ),
        "Code identity": run.manifest.code_sha256,
        "Catalog identity": run.manifest.catalog_sha256,
    }
    return (
        f"<section><h2>{text(heading)}</h2><dl>"
        + "".join(
            f"<dt>{text(label)}</dt><dd>{text(value)}</dd>" for label, value in values.items()
        )
        + "</dl><p class=small>Request durations exclude setup and report generation. "
        "Pinecone database query charges are excluded from the model reservation.</p></section>"
    )


def _marker_cell(found: Mapping[str, list[int]], *, answer: bool) -> str:
    if not answer:
        label = badge("Manual review", "gray")
    else:
        count = sum(bool(ranks) for ranks in found.values())
        color = "green" if found and count == len(found) else "orange" if count else "red"
        label = badge(f"{count}/{len(found)} markers", color)
    detail = "; ".join(
        f"{name}: {', '.join(map(str, ranks)) if ranks else 'missing'}"
        for name, ranks in found.items()
    )
    return label + (f"<br><small>{text(detail)}</small>" if detail else "")


def render_run(run: SavedRun) -> str:
    observations = {item.key: item for item in run.observations}
    selections = {selection_key(row): row for row in run.scored.rows} if run.scored else {}
    cases = {case.id: case for case in run.definition.cases}
    source_labels = {
        source.alias: source.alias
        if source.alias in run.definition.catalog.sources
        else f"Other source {index}"
        for index, source in enumerate(run.scored.snapshot.sources if run.scored else (), 1)
    }
    lines = []
    for key in run.expected_keys:
        case_id, mode, repetition = key
        observed, row = observations.get(key), selections.get(key)
        execution = (
            badge("Not run", "gray")
            if observed is None
            else badge(
                "Execution failed" if observed.status == "failed" else "Recorded",
                "red" if observed.status == "failed" else "gray",
            )
        )
        markers = (
            badge("No context recorded", "gray")
            if row is None
            else _marker_cell(
                evidence_ranks(cases[case_id], row),
                answer=cases[case_id].kind == "answer",
            )
        )
        locations = (
            "; ".join(
                f"{source_labels[item.source]} p.{item.page}"
                if item.page is not None
                else source_labels[item.source]
                for item in row.selected
            )
            if row
            else ""
        )
        values = (
            text(f"{case_id} · {mode or 'research'} · repeat {repetition}"),
            execution,
            markers,
            text(observed.status if observed else "Not run"),
            text(f"{observed.elapsed_ms} ms" if observed else "—"),
            text(locations or "No selected passages"),
        )
        lines.append("<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>")
    body = metadata(run, "Recorded configuration and execution")
    body += "<section><h2>Per-case results</h2><p class=small>Model outcomes are recorded states, "
    body += (
        "not independent quality judgments. Company cards and title hits "
        "remain in results.jsonl.</p>"
    )
    body += (
        "<div class=scroll><table><thead><tr>"
        + "".join(
            f"<th>{label}</th>"
            for label in (
                "Case / mode",
                "Execution",
                "Retrieval evidence",
                "Recorded outcome",
                "Duration",
                "Selected sources",
            )
        )
        + "</tr></thead><tbody>"
        + "".join(lines)
        + "</tbody></table></div></section>"
    )
    return document("Evaluation report", body)


def render_comparison(baseline: SavedRun, candidate: SavedRun) -> str:
    if baseline.scored is None or candidate.scored is None:
        raise RunArtifactError("comparison requires recorded selections in both runs")
    if baseline.manifest.status != "completed" or candidate.manifest.status != "completed":
        raise RunArtifactError("comparison requires completed runs")
    if len(baseline.scored.rows) != len(baseline.expected_keys) or len(
        candidate.scored.rows
    ) != len(candidate.expected_keys):
        raise RunArtifactError("comparison requires every planned context in both runs")
    comparison = compare_saved(candidate.scored, baseline.scored)
    changes = comparison["comparisons"]
    if not isinstance(changes, list):
        raise RunArtifactError("comparison did not produce rows")
    before = {selection_key(row): row for row in baseline.scored.rows}
    after = {selection_key(row): row for row in candidate.scored.rows}
    cases = {case.id: case for case in candidate.definition.cases}
    bt = {item.key: item.elapsed_ms for item in baseline.observations}
    ct = {item.key: item.elapsed_ms for item in candidate.observations}
    bs = {item.key: item.status for item in baseline.observations}
    cs = {item.key: item.status for item in candidate.observations}
    lines = []
    for change in changes:
        key = change["case"], change["archive_mode"], change["repetition"]
        old, new = before[key], after[key]
        answer = cases[key[0]].kind == "answer"
        delta = (
            "Gained: "
            + ", ".join(change["gained_evidence_markers"] or ["none"])
            + "; lost: "
            + ", ".join(change["lost_evidence_markers"] or ["none"])
        )
        ranks = "; ".join(
            f"{name}: {value['before']} → {value['after']}"
            for name, value in change["rank_changes"].items()
        )
        pool = (
            "not recorded"
            if change["same_candidate_pool"] is None
            else str(change["same_candidate_pool"])
        )
        values = (
            text(f"{key[0]} · {key[1] or 'research'} · repeat {key[2]}"),
            _marker_cell(evidence_ranks(cases[key[0]], old), answer=answer),
            _marker_cell(evidence_ranks(cases[key[0]], new), answer=answer),
            text(delta + ("; ranks: " + ranks if ranks else "")),
            text(f"+{len(change['added_passages'])} / -{len(change['removed_passages'])}"),
            text(f"{bt.get(key, '—')} → {ct.get(key, '—')} ms"),
            text(f"{bs[key]} → {cs[key]}"),
            text(pool),
        )
        lines.append("<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>")
    body = metadata(baseline, "Baseline") + metadata(candidate, "Candidate")
    body += "<section><h2>Recorded differences</h2><p>Observed differences do not prove a causal "
    body += (
        "improvement or better answers. Inspect model/configuration versions "
        "and candidate-pool control.</p>"
    )
    body += (
        "<div class=scroll><table><thead><tr>"
        + "".join(
            f"<th>{label}</th>"
            for label in (
                "Case / mode",
                "Before",
                "After",
                "Evidence changes",
                "Added / removed passages",
                "Duration",
                "Recorded outcome",
                "Same candidate pool",
            )
        )
        + "</tr></thead><tbody>"
        + "".join(lines)
        + "</tbody></table></div></section>"
    )
    return document("Evaluation comparison", body)


def write_report(reference: str, rendered: str) -> Path:
    path = checked_repository_file(reference, subtree="outputs", suffixes=(".html",))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(rendered)
    return path
