"""Unit tests for fail-closed citation-span authorization."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast
from uuid import UUID

import pytest

from investment_research.modules.documents.domain import (
    DocumentLifecycleState,
    SourceLocatorKind,
)
from investment_research.modules.evidence.application import (
    EvidenceSpanUnavailableError,
    ResolveEvidenceSpanService,
)
from investment_research.modules.evidence.contracts import CanonicalEvidenceSpanSource
from investment_research.modules.evidence.domain import EvidenceSpan
from investment_research.modules.evidence.ports import (
    CanonicalEvidenceSpanReader,
    EvidenceReadUnitOfWork,
    EvidenceReadUnitOfWorkFactory,
)
from investment_research.modules.identity.domain import WorkspaceContext

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
FOREIGN_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
SPAN_ID = UUID("10000000-0000-4000-8000-000000000001")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000002")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000003")
OTHER_VERSION_ID = UUID("10000000-0000-4000-8000-000000000004")
BLOCK_ID = UUID("10000000-0000-4000-8000-000000000005")
CREATED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
EVIDENCE_TEXT = "FY2025 revenue was $184.2 million."


class StaticEvidenceSpanReader:
    """Return one configured canonical snapshot and record lookup scope."""

    def __init__(self, result: CanonicalEvidenceSpanSource | None) -> None:
        self.result = result
        self.calls: list[tuple[WorkspaceContext, UUID]] = []

    def get_span_source(
        self,
        context: WorkspaceContext,
        evidence_span_id: UUID,
    ) -> CanonicalEvidenceSpanSource | None:
        """Record and return the configured snapshot."""

        self.calls.append((context, evidence_span_id))
        return self.result


class RecordingEvidenceReadUnitOfWork:
    """Expose one reader and record the short-lived read boundary."""

    def __init__(self, result: CanonicalEvidenceSpanSource | None) -> None:
        self.reader = StaticEvidenceSpanReader(result)
        self.evidence_spans = cast(CanonicalEvidenceSpanReader, self.reader)
        self.enter_count = 0
        self.exit_count = 0

    @contextmanager
    def open(self) -> Iterator[EvidenceReadUnitOfWork]:
        """Yield this test unit of work without write semantics."""

        self.enter_count += 1
        try:
            yield self
        finally:
            self.exit_count += 1


def make_context() -> WorkspaceContext:
    """Return the trusted workspace used by the resolver."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_span(*, workspace_id: UUID = WORKSPACE_ID) -> EvidenceSpan:
    """Build one internally valid exact evidence span."""

    return EvidenceSpan(
        id=SPAN_ID,
        workspace_id=workspace_id,
        document_version_id=VERSION_ID,
        structure_block_id=BLOCK_ID,
        source_locator_kind=SourceLocatorKind.PDF_PAGE,
        page_number=2,
        source_start_character=None,
        source_end_character=None,
        start_character=0,
        end_character=len(EVIDENCE_TEXT),
        exact_text=EVIDENCE_TEXT,
        text_sha256=sha256(EVIDENCE_TEXT.encode()).hexdigest(),
        created_at=CREATED_AT,
    )


def make_source() -> CanonicalEvidenceSpanSource:
    """Build one eligible canonical citation snapshot."""

    return CanonicalEvidenceSpanSource(
        span=make_span(),
        document_id=DOCUMENT_ID,
        document_lifecycle_state=DocumentLifecycleState.ACTIVE,
        document_current_version_id=VERSION_ID,
        linked_to_streamed_answer=True,
    )


def make_service(
    source: CanonicalEvidenceSpanSource | None,
) -> tuple[ResolveEvidenceSpanService, RecordingEvidenceReadUnitOfWork]:
    """Build the resolver around an observable read unit of work."""

    unit_of_work = RecordingEvidenceReadUnitOfWork(source)
    factory: EvidenceReadUnitOfWorkFactory = unit_of_work.open
    return ResolveEvidenceSpanService(factory), unit_of_work


def test_resolve_returns_only_a_current_linked_workspace_span() -> None:
    service, unit_of_work = make_service(make_source())

    result = service.resolve(make_context(), SPAN_ID)

    assert result.document_id == DOCUMENT_ID
    assert result.span == make_span()
    assert unit_of_work.reader.calls == [(make_context(), SPAN_ID)]
    assert unit_of_work.enter_count == unit_of_work.exit_count == 1


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(None, id="unknown"),
        pytest.param(
            make_source().model_copy(update={"span": make_span(workspace_id=FOREIGN_WORKSPACE_ID)}),
            id="foreign-workspace",
        ),
        pytest.param(
            make_source().model_copy(update={"document_current_version_id": OTHER_VERSION_ID}),
            id="inactive-version",
        ),
        pytest.param(
            make_source().model_copy(update={"linked_to_streamed_answer": False}),
            id="unlinked-span",
        ),
        pytest.param(
            make_source().model_copy(update={"document_lifecycle_state": "DELETED"}),
            id="inactive-document",
        ),
    ],
)
def test_resolve_hides_every_unavailable_reason(
    source: CanonicalEvidenceSpanSource | None,
) -> None:
    service, unit_of_work = make_service(source)

    with pytest.raises(EvidenceSpanUnavailableError, match="evidence span is unavailable"):
        service.resolve(make_context(), SPAN_ID)

    assert unit_of_work.reader.calls == [(make_context(), SPAN_ID)]
    assert unit_of_work.enter_count == unit_of_work.exit_count == 1
