"""Prepare scoped pilot requests and inspect captured app streams without network access."""

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, TypeAdapter

from investment_research.api.contracts import (
    AnswerCitationEvent,
    AnswerCompletedEvent,
    AnswerInsufficientEvent,
    AnswerStatementEvent,
    ResearchContextEvent,
    ResearchFailedEvent,
    ResearchRequestContext,
    ResearchRequestInput,
    ResearchStreamEvent,
)
from investment_research.modules.research.contracts import ResearchModel
from investment_research.modules.search.contracts import M1_PROVIDER_MAX_ATTEMPTS
from scripts.score_portfolio_retrieval import (
    DEFAULT_CATALOG,
    Case,
    Catalog,
    matches_target,
    normalized,
    validate_catalog,
)

PILOT_CASES = ("A05", "A06", "A09", "U03")
TERMINAL = (AnswerCompletedEvent, AnswerInsufficientEvent, ResearchFailedEvent)
STREAM_EVENT = TypeAdapter[ResearchStreamEvent](ResearchStreamEvent)


class PilotInputError(ValueError):
    """Report invalid pilot inputs without including captured content."""


class SourceVersion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    document_id: UUID
    document_version_id: UUID


SOURCE_MAP = TypeAdapter[dict[str, SourceVersion]](dict[str, SourceVersion])


def scoped_sources(case: Case, source_map: dict[str, SourceVersion]) -> dict[str, SourceVersion]:
    if missing := set(case.document_scope) - source_map.keys():
        raise PilotInputError(
            f"{case.id}: required source aliases missing: {', '.join(sorted(missing))}"
        )
    scope = {alias: source_map[alias] for alias in case.document_scope}
    if len({source.document_id for source in scope.values()}) != len(scope):
        raise PilotInputError(f"{case.id}: scoped document IDs must be unique")
    return scope


def request_for(case: Case, source_map: dict[str, SourceVersion]) -> ResearchRequestInput:
    scope = scoped_sources(case, source_map)
    return ResearchRequestInput(
        question=case.question,
        context=ResearchRequestContext(
            document_ids=tuple(source.document_id for source in scope.values())
        ),
        generation_model=ResearchModel.TERRA,
        support_model=ResearchModel.TERRA,
        decompose_query=False,
        rerank_with_luna=False,
    )


def parse_events(stream: str) -> list[ResearchStreamEvent]:
    events = [
        STREAM_EVENT.validate_json("\n".join(lines))
        for block in stream.replace("\r\n", "\n").split("\n\n")
        if (
            lines := [
                line[5:].removeprefix(" ")
                for line in block.splitlines()
                if line.startswith("data:")
            ]
        )
    ]
    if not events or len({event.research_request_id for event in events}) != 1:
        raise PilotInputError("capture must contain exactly one research request")
    if not isinstance(events[-1], TERMINAL) or any(
        isinstance(event, TERMINAL) for event in events[:-1]
    ):
        raise PilotInputError("capture must end with exactly one terminal event")
    return events


def inspect_capture(
    case: Case, source_map: dict[str, SourceVersion], stream: str
) -> dict[str, object]:
    scope = scoped_sources(case, source_map)
    aliases = {
        (source.document_id, source.document_version_id): alias for alias, source in scope.items()
    }
    events = parse_events(stream)
    contexts = [event for event in events if isinstance(event, ResearchContextEvent)]
    if len(contexts) > 1:
        raise PilotInputError("capture contains duplicate model context")
    terminal = events[-1]
    if isinstance(terminal, AnswerCompletedEvent) and not contexts:
        raise PilotInputError("completed answer has no captured model context")
    passages = []
    for source in contexts[0].sources if contexts else ():
        alias = aliases.get((source.document_id, source.document_version_id))
        if not alias:
            raise PilotInputError("captured passage belongs to an unexpected document or version")
        passages.append((alias, source.page_number, normalized(source.text)))
    if len(passages) > 8:
        raise PilotInputError("captured context exceeds the pilot's eight-passage limit")
    statements = [event for event in events if isinstance(event, AnswerStatementEvent)]
    citations = [event for event in events if isinstance(event, AnswerCitationEvent)]
    if isinstance(terminal, AnswerCompletedEvent):
        statement_ids = {event.statement_id for event in statements}
        if (
            not statement_ids
            or len(statement_ids) != len(statements)
            or ({event.statement_id for event in citations} != statement_ids)
        ):
            raise PilotInputError("completed answer lacks valid statement-to-citation links")
    elif statements or citations:
        raise PilotInputError("failed or insufficient case emitted answer content")
    return {
        "case": case.id,
        "request_id": str(terminal.research_request_id),
        "terminal_event": terminal.event,
        "coverage": terminal.coverage
        if isinstance(terminal, (AnswerCompletedEvent, AnswerInsufficientEvent))
        else None,
        "context_event_seen": bool(contexts),
        "selected_passages": len(passages),
        "evidence_marker_ranks": {
            target.id: [
                rank
                for rank, (alias, page, text) in enumerate(passages, start=1)
                if alias == target.source and matches_target(target, page, text)
            ]
            for target in case.context_targets
        },
        "statements": len(statements),
        "citations": len(citations),
        "u03_abstained": isinstance(terminal, AnswerInsufficientEvent)
        if case.id == "U03"
        else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", type=Path, help="Local source-alias to document/version map")
    parser.add_argument("--case", choices=PILOT_CASES, help="Prepare one request body")
    parser.add_argument(
        "--stream", type=Path, help="Inspect a saved SSE capture for the selected case"
    )
    args = parser.parse_args()
    if args.stream and not args.case:
        parser.error("--stream requires --case")

    try:
        catalog = Catalog.model_validate_json(DEFAULT_CATALOG.read_bytes())
        validate_catalog(catalog)
        source_map = SOURCE_MAP.validate_json(args.sources.read_bytes())
        all_cases = {case.id: case for case in catalog.cases}
        if args.stream:
            result: object = inspect_capture(
                all_cases[args.case], source_map, args.stream.read_text(encoding="utf-8")
            )
        elif args.case:
            result = request_for(all_cases[args.case], source_map).model_dump(mode="json")
        else:
            result = {
                "requests": {
                    case_id: request_for(all_cases[case_id], source_map).model_dump(mode="json")
                    for case_id in PILOT_CASES
                },
                "maximum_provider_attempts": {
                    "openai_query_embedding": len(PILOT_CASES) * M1_PROVIDER_MAX_ATTEMPTS,
                    "pinecone_query": len(PILOT_CASES) * M1_PROVIDER_MAX_ATTEMPTS,
                    "openai_answer": len(PILOT_CASES),
                    "openai_batched_support": len(PILOT_CASES),
                },
            }
    except PilotInputError as error:
        print(f"Pilot input rejected: {error}", file=sys.stderr)
        return 1
    except OSError, ValueError:
        print("Pilot input rejected: invalid or unreadable local file", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
