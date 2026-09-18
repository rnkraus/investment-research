"""Add the resumable M1 dense-indexing lifecycle.

Revision ID: 0006_m1_indexing_lifecycle
Revises: 0005_m1_indexing_config
Create Date: 2026-09-18 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_m1_indexing_lifecycle"
down_revision: str | Sequence[str] | None = "0005_m1_indexing_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

M0_EMBEDDING_CONFIG_VERSION = "m0.0-embedding-disabled-v1"
M0_DENSE_INDEX_CONFIG_VERSION = "m0.0-dense-index-disabled-v1"


def _m1_consistent_state_constraint() -> str:
    """Return the lifecycle invariant shared with the SQLAlchemy mapping."""

    return (
        "(state = 'CAPTURED' AND started_at IS NULL AND completed_at IS NULL "
        "AND error_summary IS NULL AND attempt_count = 0 "
        "AND lease_id IS NULL AND lease_expires_at IS NULL) OR "
        "(state = 'PARSING' AND started_at IS NOT NULL AND completed_at IS NULL "
        "AND error_summary IS NULL AND attempt_count > 0 "
        "AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
        "(state = 'LEXICAL_READY' AND started_at IS NOT NULL "
        "AND error_summary IS NULL AND attempt_count > 0 AND ((completed_at IS NOT NULL "
        "AND lease_id IS NULL AND lease_expires_at IS NULL) OR "
        "(completed_at IS NULL AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL "
        f"AND embedding_config_version <> '{M0_EMBEDDING_CONFIG_VERSION}' "
        f"AND dense_index_config_version <> '{M0_DENSE_INDEX_CONFIG_VERSION}'))) OR "
        "(state = 'INDEXING' AND started_at IS NOT NULL AND completed_at IS NULL "
        "AND error_summary IS NULL AND attempt_count > 0 "
        "AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL "
        f"AND embedding_config_version <> '{M0_EMBEDDING_CONFIG_VERSION}' "
        f"AND dense_index_config_version <> '{M0_DENSE_INDEX_CONFIG_VERSION}') OR "
        "(state = 'SEARCHABLE' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND error_summary IS NULL AND attempt_count > 0 "
        "AND lease_id IS NULL AND lease_expires_at IS NULL "
        f"AND embedding_config_version <> '{M0_EMBEDDING_CONFIG_VERSION}' "
        f"AND dense_index_config_version <> '{M0_DENSE_INDEX_CONFIG_VERSION}') OR "
        "(state = 'FAILED' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND char_length(btrim(error_summary)) > 0 AND attempt_count > 0 "
        "AND lease_id IS NULL AND lease_expires_at IS NULL)"
    )


def _m0_consistent_state_constraint() -> str:
    """Return the lifecycle invariant that existed at revision 0005."""

    return (
        "(state = 'CAPTURED' AND started_at IS NULL AND completed_at IS NULL "
        "AND error_summary IS NULL AND attempt_count = 0 "
        "AND lease_id IS NULL AND lease_expires_at IS NULL) OR "
        "(state = 'PARSING' AND started_at IS NOT NULL AND completed_at IS NULL "
        "AND error_summary IS NULL AND attempt_count > 0 "
        "AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
        "(state = 'LEXICAL_READY' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND error_summary IS NULL AND attempt_count > 0 "
        "AND lease_id IS NULL AND lease_expires_at IS NULL) OR "
        "(state = 'FAILED' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND char_length(btrim(error_summary)) > 0 AND attempt_count > 0 "
        "AND lease_id IS NULL AND lease_expires_at IS NULL)"
    )


def upgrade() -> None:
    """Permit resumable dense indexing while retaining historical terminal runs."""

    op.drop_constraint(
        op.f("ck_processing_runs_consistent_state"),
        "processing_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_processing_runs_processing_state"),
        "processing_runs",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_processing_runs_coherent_dense_index_configuration"),
        "processing_runs",
        "(embedding_config_version = "
        f"'{M0_EMBEDDING_CONFIG_VERSION}' AND dense_index_config_version = "
        f"'{M0_DENSE_INDEX_CONFIG_VERSION}') OR (embedding_config_version <> "
        f"'{M0_EMBEDDING_CONFIG_VERSION}' AND dense_index_config_version <> "
        f"'{M0_DENSE_INDEX_CONFIG_VERSION}')",
    )
    op.create_check_constraint(
        op.f("ck_processing_runs_consistent_state"),
        "processing_runs",
        _m1_consistent_state_constraint(),
    )
    op.create_check_constraint(
        op.f("ck_processing_runs_processing_state"),
        "processing_runs",
        "state IN ('CAPTURED', 'PARSING', 'LEXICAL_READY', 'INDEXING', 'SEARCHABLE', 'FAILED')",
    )


def downgrade() -> None:
    """Restore the terminal lexical-only lifecycle from revision 0005."""

    op.drop_constraint(
        op.f("ck_processing_runs_processing_state"),
        "processing_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_processing_runs_consistent_state"),
        "processing_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_processing_runs_coherent_dense_index_configuration"),
        "processing_runs",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_processing_runs_consistent_state"),
        "processing_runs",
        _m0_consistent_state_constraint(),
    )
    op.create_check_constraint(
        op.f("ck_processing_runs_processing_state"),
        "processing_runs",
        "state IN ('CAPTURED', 'PARSING', 'LEXICAL_READY', 'FAILED')",
    )
