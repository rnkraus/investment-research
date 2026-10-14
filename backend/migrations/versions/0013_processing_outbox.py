"""Persist scoped processing delivery intent without changing capture responses.

Revision ID: 0013_processing_outbox
Revises: 0012_rag_context_bounds
"""

from collections.abc import Sequence
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_processing_outbox"
down_revision: str | Sequence[str] | None = "0012_rag_context_bounds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep migration SQL independent of mutable application mappings.
CHECKS = (
    ("schema_version = 'outbox-event.v1'", "schema_version"),
    ("kind = 'PROCESS_DOCUMENT'", "supported_kind"),
    ("generation BETWEEN 0 AND 2", "bounded_generation"),
    ("attempt_count BETWEEN 0 AND 3", "bounded_attempt_count"),
    (
        "state IN ('PENDING', 'CLAIMED', 'RETRY_WAIT', 'PUBLISHED', 'BLOCKED', 'CANCELLED')",
        "delivery_state",
    ),
    (
        "last_error_category IS NULL OR last_error_category IN "
        "('DEPENDENCY_UNAVAILABLE', 'INVALID_JOB', 'SOURCE_INELIGIBLE', 'ATTEMPTS_EXHAUSTED')",
        "safe_error_category",
    ),
    (
        "(state = 'CANCELLED' AND processing_run_id IS NULL) OR "
        "(processing_run_id IS NOT NULL AND processing_run_id = target_id)",
        "scoped_target_binding",
    ),
    (
        "(next_attempt_at IS NULL OR next_attempt_at >= created_at) AND "
        "(published_at IS NULL OR published_at >= created_at)",
        "ordered_times",
    ),
    (
        "(state = 'CLAIMED' AND attempt_count > 0 AND lease_id IS NOT NULL AND "
        "lease_expires_at > created_at AND lease_expires_at IS NOT NULL) OR "
        "(state <> 'CLAIMED' AND lease_id IS NULL AND lease_expires_at IS NULL)",
        "consistent_lease",
    ),
    (
        "(state IN ('PENDING', 'RETRY_WAIT') AND next_attempt_at IS NOT NULL) OR "
        "(state NOT IN ('PENDING', 'RETRY_WAIT') AND next_attempt_at IS NULL)",
        "consistent_due_time",
    ),
    (
        "state <> 'PENDING' OR (attempt_count = 0 AND last_error_category IS NULL)",
        "initial_pending_state",
    ),
    (
        "state <> 'RETRY_WAIT' OR (attempt_count BETWEEN 1 AND 2 AND "
        "last_error_category IS NOT NULL AND last_error_category = 'DEPENDENCY_UNAVAILABLE')",
        "bounded_retry_wait",
    ),
    (
        "(state = 'PUBLISHED' AND published_at IS NOT NULL AND attempt_count > 0) OR "
        "state = 'CANCELLED' OR (state <> 'PUBLISHED' AND published_at IS NULL)",
        "consistent_publication",
    ),
    ("state <> 'BLOCKED' OR last_error_category IS NOT NULL", "blocked_reason"),
)


def upgrade() -> None:
    table = op.create_table(
        "outbox_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("processing_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_category", sa.String(32), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("workspace_id", "event_id"),
        sa.UniqueConstraint("workspace_id", "kind", "target_id", "generation"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "processing_run_id"],
            ["processing_runs.workspace_id", "processing_runs.id"],
            ondelete="RESTRICT",
        ),
        *(sa.CheckConstraint(expression, name=name) for expression, name in CHECKS),
    )
    op.create_index(
        "ix_outbox_events_due", "outbox_events", ["state", "next_attempt_at", "event_id"]
    )
    op.create_index(
        "ix_outbox_events_processing_target", "outbox_events", ["workspace_id", "processing_run_id"]
    )
    connection = op.get_bind()
    # Stream the legacy snapshot; never enqueue active, failed, terminal, or excluded work.
    rows = connection.execute(
        sa.text(
            "SELECT r.workspace_id, r.id, r.created_at FROM processing_runs r "
            "JOIN document_versions v ON v.workspace_id = r.workspace_id "
            "AND v.id = r.document_version_id "
            "JOIN documents d ON d.workspace_id = v.workspace_id AND d.id = v.document_id "
            "AND d.current_version_id = v.id "
            "WHERE d.lifecycle_state = 'ACTIVE' AND r.state = 'CAPTURED' "
            "AND r.attempt_count = 0 AND r.started_at IS NULL AND r.completed_at IS NULL "
            "AND r.lease_id IS NULL AND r.lease_expires_at IS NULL ORDER BY r.workspace_id, r.id"
        ).execution_options(stream_results=True, max_row_buffer=100)
    ).mappings()
    while batch := rows.fetchmany(100):
        connection.execute(
            table.insert(),
            [
                {
                    "event_id": uuid5(
                        NAMESPACE_URL,
                        f"investment-research:outbox-event.v1:{row['workspace_id']}:"
                        f"PROCESS_DOCUMENT:{row['id']}:0",
                    ),
                    "workspace_id": row["workspace_id"],
                    "schema_version": "outbox-event.v1",
                    "kind": "PROCESS_DOCUMENT",
                    "target_id": row["id"],
                    "processing_run_id": row["id"],
                    "generation": 0,
                    "created_at": row["created_at"],
                    "state": "PENDING",
                    "attempt_count": 0,
                    "next_attempt_at": row["created_at"],
                }
                for row in batch
            ],
        )


def downgrade() -> None:
    """Never silently discard live intent or detached publication history."""

    if op.get_bind().scalar(sa.text("SELECT 1 FROM outbox_events LIMIT 1")):
        raise RuntimeError("Cannot downgrade: durable delivery intent or history exists")
    op.drop_index("ix_outbox_events_processing_target", table_name="outbox_events")
    op.drop_index("ix_outbox_events_due", table_name="outbox_events")
    op.drop_table("outbox_events")
