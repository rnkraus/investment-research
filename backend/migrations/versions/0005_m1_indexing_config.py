"""Add immutable M1 indexing configuration snapshots.

Revision ID: 0005_m1_indexing_config
Revises: 0004_m1_text_sources_and_anchors
Create Date: 2026-09-18 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_m1_indexing_config"
down_revision: str | Sequence[str] | None = "0004_m1_text_sources_and_anchors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

M0_EMBEDDING_CONFIG_VERSION = "m0.0-embedding-disabled-v1"
M0_DENSE_INDEX_CONFIG_VERSION = "m0.0-dense-index-disabled-v1"
M1_LEXICAL_CONFIG_VERSION = "m1-english-lexical-v1"
M1_EMBEDDING_CONFIG_VERSION = "openai-text-embedding-3-small-1536-v1"
M1_DENSE_INDEX_CONFIG_VERSION = "pinecone-serverless-cosine-1536-aws-us-east-1-v1"


def upgrade() -> None:
    """Backfill immutable indexing versions before enforcing non-null snapshots."""

    op.add_column(
        "processing_runs",
        sa.Column("embedding_config_version", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "processing_runs",
        sa.Column("dense_index_config_version", sa.String(length=128), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE processing_runs SET "
            "embedding_config_version = CASE "
            "WHEN lexical_config_version = :m1_lexical THEN :m1_embedding "
            "ELSE :m0_embedding END, "
            "dense_index_config_version = CASE "
            "WHEN lexical_config_version = :m1_lexical THEN :m1_dense_index "
            "ELSE :m0_dense_index END"
        ).bindparams(
            m1_lexical=M1_LEXICAL_CONFIG_VERSION,
            m1_embedding=M1_EMBEDDING_CONFIG_VERSION,
            m0_embedding=M0_EMBEDDING_CONFIG_VERSION,
            m1_dense_index=M1_DENSE_INDEX_CONFIG_VERSION,
            m0_dense_index=M0_DENSE_INDEX_CONFIG_VERSION,
        )
    )
    op.alter_column(
        "processing_runs",
        "embedding_config_version",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    op.alter_column(
        "processing_runs",
        "dense_index_config_version",
        existing_type=sa.String(length=128),
        nullable=False,
    )

    op.drop_constraint(
        op.f("ck_processing_runs_non_empty_configuration_versions"),
        "processing_runs",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_processing_runs_non_empty_configuration_versions"),
        "processing_runs",
        "char_length(btrim(parser_version)) > 0 "
        "AND char_length(btrim(parser_config_version)) > 0 "
        "AND char_length(btrim(chunker_version)) > 0 "
        "AND char_length(btrim(chunking_config_version)) > 0 "
        "AND char_length(btrim(lexical_config_version)) > 0 "
        "AND char_length(btrim(embedding_config_version)) > 0 "
        "AND char_length(btrim(dense_index_config_version)) > 0",
    )


def downgrade() -> None:
    """Remove M1 indexing snapshots while restoring the prior configuration check."""

    op.drop_constraint(
        op.f("ck_processing_runs_non_empty_configuration_versions"),
        "processing_runs",
        type_="check",
    )
    op.drop_column("processing_runs", "dense_index_config_version")
    op.drop_column("processing_runs", "embedding_config_version")
    op.create_check_constraint(
        op.f("ck_processing_runs_non_empty_configuration_versions"),
        "processing_runs",
        "char_length(btrim(parser_version)) > 0 "
        "AND char_length(btrim(parser_config_version)) > 0 "
        "AND char_length(btrim(chunker_version)) > 0 "
        "AND char_length(btrim(chunking_config_version)) > 0 "
        "AND char_length(btrim(lexical_config_version)) > 0",
    )
