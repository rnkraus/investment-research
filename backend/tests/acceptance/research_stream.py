"""Shared research-stream assertions for the current M0 and M1 runtime journeys."""

import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from investment_research.api.contracts import ResearchContextEvent


def parse_answer_events(body: str, *, document_ids: Sequence[str | UUID]) -> list[dict[str, Any]]:
    """Validate selected context separately without hiding unexpected answer events."""

    allowed_documents = {str(identifier) for identifier in document_ids}
    events: list[dict[str, Any]] = []
    context_request_id: str | None = None
    for frame in body.strip().split("\n\n"):
        lines = frame.splitlines()
        if not lines or lines[0].startswith(":") or lines[0] == "event: research.progress":
            continue
        event_name = lines[0].removeprefix("event: ")
        payload = json.loads(lines[1].removeprefix("data: "))
        if event_name == "research.context":
            assert context_request_id is None, "Selected context must be emitted at most once"
            assert not events, "Selected context must precede answer events"
            context = ResearchContextEvent.model_validate(payload)
            assert all(
                str(source.document_id) in allowed_documents for source in context.sources
            ), "Selected context must remain within the requested document scope"
            context_request_id = str(context.research_request_id)
            continue
        if context_request_id is not None:
            assert payload["research_request_id"] == context_request_id, (
                "Context and answer must belong to the same research request"
            )
        events.append({"event": event_name, "data": payload})
    if any(
        event["event"] in {"answer.statement", "answer.citation", "answer.completed"}
        for event in events
    ):
        assert context_request_id is not None, "An accepted answer must have prepared context"
    return events
