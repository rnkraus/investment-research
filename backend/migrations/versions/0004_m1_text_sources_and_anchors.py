"""Add M1 text sources and stable anchors.

Revision ID: 0004_m1_text_sources_and_anchors
Revises: 0003_processing_execution_lease
Create Date: 2026-09-17 01:55:48.294159

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_m1_text_sources_and_anchors"
down_revision: str | Sequence[str] | None = "0003_processing_execution_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Backfill M0 PDF rows before enforcing the M1 locator shapes."""

    op.drop_constraint(
        op.f("ck_original_artifacts_original_media_type"),
        "original_artifacts",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_original_artifacts_original_media_type"),
        "original_artifacts",
        "media_type IN ('application/pdf', 'text/plain')",
    )

    locator_type = sa.Enum(
        "PDF_PAGE",
        "TEXT_PASSAGE",
        name="source_locator_kind",
        native_enum=False,
        length=32,
    )
    op.add_column(
        "structure_blocks",
        sa.Column("source_locator_kind", locator_type, nullable=True),
    )
    op.add_column(
        "evidence_spans",
        sa.Column("source_locator_kind", locator_type, nullable=True),
    )
    op.add_column(
        "evidence_spans",
        sa.Column("source_start_character", sa.Integer(), nullable=True),
    )
    op.add_column(
        "evidence_spans",
        sa.Column("source_end_character", sa.Integer(), nullable=True),
    )
    op.execute(sa.text("UPDATE structure_blocks SET source_locator_kind = 'PDF_PAGE'"))
    op.execute(sa.text("UPDATE evidence_spans SET source_locator_kind = 'PDF_PAGE'"))
    op.alter_column(
        "structure_blocks",
        "source_locator_kind",
        existing_type=locator_type,
        nullable=False,
    )
    op.alter_column(
        "evidence_spans",
        "source_locator_kind",
        existing_type=locator_type,
        nullable=False,
    )

    op.alter_column(
        "structure_blocks",
        "page_number",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.drop_constraint(
        op.f("ck_structure_blocks_positive_page_number"),
        "structure_blocks",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_structure_blocks_matching_text_length"),
        "structure_blocks",
        "char_length(text) = end_character - start_character",
    )
    op.create_check_constraint(
        op.f("ck_structure_blocks_valid_source_locator"),
        "structure_blocks",
        "(source_locator_kind = 'PDF_PAGE' AND page_number > 0) OR "
        "(source_locator_kind = 'TEXT_PASSAGE' AND page_number IS NULL)",
    )

    op.alter_column(
        "evidence_spans",
        "page_number",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.drop_constraint(
        op.f("ck_evidence_spans_positive_page_number"),
        "evidence_spans",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_evidence_spans_valid_source_locator"),
        "evidence_spans",
        "(source_locator_kind = 'PDF_PAGE' AND page_number > 0 "
        "AND source_start_character IS NULL AND source_end_character IS NULL) OR "
        "(source_locator_kind = 'TEXT_PASSAGE' AND page_number IS NULL "
        "AND source_start_character >= 0 "
        "AND source_end_character > source_start_character "
        "AND source_end_character - source_start_character = char_length(exact_text))",
    )


def downgrade() -> None:
    """Restore the PDF-only schema only when no M1 text data would be lost."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM original_artifacts WHERE media_type <> 'application/pdf') "
            "OR EXISTS (SELECT 1 FROM structure_blocks "
            "WHERE source_locator_kind <> 'PDF_PAGE') "
            "OR EXISTS (SELECT 1 FROM evidence_spans "
            "WHERE source_locator_kind <> 'PDF_PAGE') "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade while M1 text sources or anchors exist'; "
            "END IF; END $$"
        )
    )

    op.drop_constraint(
        op.f("ck_evidence_spans_valid_source_locator"),
        "evidence_spans",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_evidence_spans_positive_page_number"),
        "evidence_spans",
        "page_number > 0",
    )
    op.alter_column(
        "evidence_spans",
        "page_number",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_column("evidence_spans", "source_end_character")
    op.drop_column("evidence_spans", "source_start_character")
    op.drop_column("evidence_spans", "source_locator_kind")

    op.drop_constraint(
        op.f("ck_structure_blocks_valid_source_locator"),
        "structure_blocks",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_structure_blocks_matching_text_length"),
        "structure_blocks",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_structure_blocks_positive_page_number"),
        "structure_blocks",
        "page_number > 0",
    )
    op.alter_column(
        "structure_blocks",
        "page_number",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_column("structure_blocks", "source_locator_kind")

    op.drop_constraint(
        op.f("ck_original_artifacts_original_media_type"),
        "original_artifacts",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_original_artifacts_original_media_type"),
        "original_artifacts",
        "media_type IN ('application/pdf')",
    )
