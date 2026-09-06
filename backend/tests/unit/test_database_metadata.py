"""Unit tests for the complete SQLAlchemy mapping registry."""

from investment_research.infrastructure.database.metadata import get_model_metadata

EXPECTED_TABLE_NAMES = {
    "answer_statements",
    "answer_validations",
    "answers",
    "chunk_sets",
    "chunks",
    "document_versions",
    "documents",
    "evidence_spans",
    "original_artifacts",
    "processing_runs",
    "research_requests",
    "retrieval_candidates",
    "retrieval_runs",
    "statement_evidence",
    "structure_blocks",
    "workspaces",
}


def test_model_metadata_registers_all_m0_0_tables() -> None:
    metadata = get_model_metadata()

    assert set(metadata.tables) == EXPECTED_TABLE_NAMES
    assert all(table.metadata is metadata for table in metadata.tables.values())
