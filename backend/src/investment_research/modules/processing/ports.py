"""Application-owned persistence contracts for document processing."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import (
    Chunk,
    ChunkSet,
    ProcessingRun,
    StructureBlock,
)


class ProcessingRepository(Protocol):
    """Persist processing state and derived lexical content within one workspace."""

    def add_run(self, context: WorkspaceContext, run: ProcessingRun) -> None:
        """Stage a newly captured processing run."""

    def get_run_by_id(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> ProcessingRun | None:
        """Return a scoped run without disclosing inaccessible identifiers."""

    def update_run(self, context: WorkspaceContext, run: ProcessingRun) -> bool:
        """Update a scoped run snapshot and report whether it existed."""

    def add_lexical_output(
        self,
        context: WorkspaceContext,
        blocks: Sequence[StructureBlock],
        chunk_set: ChunkSet,
        chunks: Sequence[Chunk],
    ) -> None:
        """Stage one internally consistent set of parsed blocks and lexical chunks."""
