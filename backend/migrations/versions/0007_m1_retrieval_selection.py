"""add M1 retrieval selection

Revision ID: 0007_m1_retrieval_selection
Revises: 0006_m1_indexing_lifecycle
Create Date: 2026-09-19 19:41:02.427778

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_m1_retrieval_selection"
down_revision: str | Sequence[str] | None = "0006_m1_indexing_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Record M1 retrieval lanes, selector configuration, and selected order."""

    op.drop_constraint(
        op.f("ck_retrieval_runs_bounded_candidate_limit"),
        "retrieval_runs",
        type_="check",
    )
    op.alter_column(
        "retrieval_runs",
        "candidate_limit",
        new_column_name="lexical_candidate_limit",
    )
    op.add_column(
        "retrieval_runs",
        sa.Column("mode", sa.String(length=32), server_default="LEXICAL", nullable=False),
    )
    op.add_column(
        "retrieval_runs",
        sa.Column("embedding_config_version", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "retrieval_runs",
        sa.Column("dense_index_config_version", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "retrieval_runs",
        sa.Column(
            "selector_config_version",
            sa.String(length=128),
            server_default="m0.0-lexical-order-v1",
            nullable=False,
        ),
    )
    op.add_column(
        "retrieval_runs",
        sa.Column("dense_candidate_limit", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "retrieval_runs",
        sa.Column("context_chunk_limit", sa.Integer(), nullable=True),
    )
    op.add_column(
        "retrieval_runs",
        sa.Column("context_character_limit", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_retrieval_runs_bounded_lexical_candidate_limit"),
        "retrieval_runs",
        "lexical_candidate_limit BETWEEN 1 AND 100",
    )
    op.create_check_constraint(
        op.f("ck_retrieval_runs_bounded_dense_candidate_limit"),
        "retrieval_runs",
        "dense_candidate_limit BETWEEN 0 AND 100",
    )
    op.create_check_constraint(
        op.f("ck_retrieval_runs_bounded_context_chunk_limit"),
        "retrieval_runs",
        "context_chunk_limit IS NULL OR context_chunk_limit BETWEEN 1 AND 8",
    )
    op.create_check_constraint(
        op.f("ck_retrieval_runs_bounded_context_character_limit"),
        "retrieval_runs",
        "context_character_limit IS NULL OR context_character_limit BETWEEN 1 AND 24000",
    )
    op.create_check_constraint(
        op.f("ck_retrieval_runs_retrieval_mode"),
        "retrieval_runs",
        "mode IN ('LEXICAL', 'DUAL_LANE')",
    )
    op.create_check_constraint(
        op.f("ck_retrieval_runs_non_empty_embedding_config_version"),
        "retrieval_runs",
        "embedding_config_version IS NULL OR char_length(btrim(embedding_config_version)) > 0",
    )
    op.create_check_constraint(
        op.f("ck_retrieval_runs_non_empty_dense_index_config_version"),
        "retrieval_runs",
        "dense_index_config_version IS NULL OR char_length(btrim(dense_index_config_version)) > 0",
    )
    op.create_check_constraint(
        op.f("ck_retrieval_runs_non_empty_selector_config_version"),
        "retrieval_runs",
        "char_length(btrim(selector_config_version)) > 0",
    )
    op.create_check_constraint(
        op.f("ck_retrieval_runs_coherent_retrieval_mode"),
        "retrieval_runs",
        "(mode = 'LEXICAL' AND embedding_config_version IS NULL "
        "AND dense_index_config_version IS NULL AND dense_candidate_limit = 0 "
        "AND context_chunk_limit IS NULL AND context_character_limit IS NULL) OR "
        "(mode = 'DUAL_LANE' AND embedding_config_version IS NOT NULL "
        "AND dense_index_config_version IS NOT NULL AND dense_candidate_limit > 0 "
        "AND context_chunk_limit IS NOT NULL AND context_character_limit IS NOT NULL)",
    )
    op.alter_column("retrieval_runs", "mode", server_default=None)
    op.alter_column("retrieval_runs", "selector_config_version", server_default=None)
    op.alter_column("retrieval_runs", "dense_candidate_limit", server_default=None)

    op.drop_constraint(
        op.f("ck_retrieval_candidates_finite_non_negative_raw_score"),
        "retrieval_candidates",
        type_="check",
    )
    op.add_column(
        "retrieval_candidates",
        sa.Column("selection_rank", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_retrieval_candidates_finite_raw_score"),
        "retrieval_candidates",
        "raw_score > '-Infinity'::double precision AND raw_score < 'Infinity'::double precision",
    )
    op.create_check_constraint(
        op.f("ck_retrieval_candidates_positive_selection_rank"),
        "retrieval_candidates",
        "selection_rank IS NULL OR selection_rank > 0",
    )
    op.create_unique_constraint(
        op.f("uq_retrieval_candidates_workspace_id_retrieval_run_id_selection_rank"),
        "retrieval_candidates",
        ["workspace_id", "retrieval_run_id", "selection_rank"],
    )


def downgrade() -> None:
    """Restore the lexical-only retrieval snapshot from revision 0006."""

    op.drop_constraint(
        op.f("uq_retrieval_candidates_workspace_id_retrieval_run_id_selection_rank"),
        "retrieval_candidates",
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_retrieval_candidates_positive_selection_rank"),
        "retrieval_candidates",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_retrieval_candidates_finite_raw_score"),
        "retrieval_candidates",
        type_="check",
    )
    op.drop_column("retrieval_candidates", "selection_rank")
    op.create_check_constraint(
        op.f("ck_retrieval_candidates_finite_non_negative_raw_score"),
        "retrieval_candidates",
        "raw_score >= 0 AND raw_score < 'Infinity'::double precision",
    )

    op.drop_constraint(
        op.f("ck_retrieval_runs_coherent_retrieval_mode"),
        "retrieval_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_retrieval_runs_non_empty_selector_config_version"),
        "retrieval_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_retrieval_runs_non_empty_dense_index_config_version"),
        "retrieval_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_retrieval_runs_non_empty_embedding_config_version"),
        "retrieval_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_retrieval_runs_retrieval_mode"),
        "retrieval_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_retrieval_runs_bounded_context_character_limit"),
        "retrieval_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_retrieval_runs_bounded_context_chunk_limit"),
        "retrieval_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_retrieval_runs_bounded_dense_candidate_limit"),
        "retrieval_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_retrieval_runs_bounded_lexical_candidate_limit"),
        "retrieval_runs",
        type_="check",
    )
    op.drop_column("retrieval_runs", "context_character_limit")
    op.drop_column("retrieval_runs", "context_chunk_limit")
    op.drop_column("retrieval_runs", "dense_candidate_limit")
    op.drop_column("retrieval_runs", "selector_config_version")
    op.drop_column("retrieval_runs", "dense_index_config_version")
    op.drop_column("retrieval_runs", "embedding_config_version")
    op.drop_column("retrieval_runs", "mode")
    op.alter_column(
        "retrieval_runs",
        "lexical_candidate_limit",
        new_column_name="candidate_limit",
    )
    op.create_check_constraint(
        op.f("ck_retrieval_runs_bounded_candidate_limit"),
        "retrieval_runs",
        "candidate_limit BETWEEN 1 AND 100",
    )
