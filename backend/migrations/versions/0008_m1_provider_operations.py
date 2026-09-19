"""Add M1 canonical provider operations.

Revision ID: 0008_m1_provider_operations
Revises: 0007_m1_retrieval_selection
Create Date: 2026-09-20 00:46:06.748781

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_m1_provider_operations"
down_revision: str | Sequence[str] | None = "0007_m1_retrieval_selection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the workspace-scoped provider-operation ledger."""

    op.create_table(
        "provider_operations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("correlation_id", sa.UUID(), nullable=False),
        sa.Column("research_request_id", sa.UUID(), nullable=True),
        sa.Column("processing_run_id", sa.UUID(), nullable=True),
        sa.Column(
            "operation",
            sa.Enum(
                "DOCUMENT_EMBEDDING",
                "QUERY_EMBEDDING",
                "ANSWER_GENERATION",
                "STATEMENT_SUPPORT",
                name="provider_operation_kind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("configuration_version", sa.String(length=128), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "STARTED",
                "SUCCEEDED",
                "FAILED",
                name="provider_operation_state",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("reserved_cost_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("actual_cost_micro_usd", sa.BigInteger(), nullable=True),
        sa.Column("input_units", sa.BigInteger(), nullable=True),
        sa.Column("output_units", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column(
            "safe_error_code",
            sa.Enum(
                "AUTHENTICATION",
                "RATE_LIMIT",
                "TIMEOUT",
                "REFUSAL",
                "CONTRACT",
                "CONFIGURATION",
                "UNAVAILABLE",
                name="provider_operation_error_code",
                native_enum=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.CheckConstraint(
            "(operation = 'DOCUMENT_EMBEDDING' "
            "AND processing_run_id IS NOT NULL AND research_request_id IS NULL) OR "
            "(operation IN ('QUERY_EMBEDDING', 'ANSWER_GENERATION', "
            "'STATEMENT_SUPPORT') "
            "AND research_request_id IS NOT NULL AND processing_run_id IS NULL)",
            name=op.f("ck_provider_operations_coherent_owner"),
        ),
        sa.CheckConstraint(
            "(state = 'STARTED' AND actual_cost_micro_usd IS NULL "
            "AND input_units IS NULL AND output_units IS NULL "
            "AND completed_at IS NULL AND duration_ms IS NULL "
            "AND safe_error_code IS NULL) OR "
            "(state = 'SUCCEEDED' AND actual_cost_micro_usd IS NOT NULL "
            "AND completed_at IS NOT NULL AND duration_ms IS NOT NULL "
            "AND safe_error_code IS NULL) OR "
            "(state = 'FAILED' AND actual_cost_micro_usd IS NOT NULL "
            "AND completed_at IS NOT NULL AND duration_ms IS NOT NULL "
            "AND safe_error_code IS NOT NULL)",
            name=op.f("ck_provider_operations_lifecycle"),
        ),
        sa.CheckConstraint(
            "configuration_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'",
            name=op.f("ck_provider_operations_configuration_version"),
        ),
        sa.CheckConstraint(
            "operation IN ('DOCUMENT_EMBEDDING', 'QUERY_EMBEDDING', "
            "'ANSWER_GENERATION', 'STATEMENT_SUPPORT')",
            name=op.f("ck_provider_operations_operation"),
        ),
        sa.CheckConstraint(
            "provider ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]*$' "
            "AND model ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]*$'",
            name=op.f("ck_provider_operations_provider_identifiers"),
        ),
        sa.CheckConstraint(
            "safe_error_code IS NULL OR safe_error_code IN "
            "('AUTHENTICATION', 'RATE_LIMIT', 'TIMEOUT', 'REFUSAL', "
            "'CONTRACT', 'CONFIGURATION', 'UNAVAILABLE')",
            name=op.f("ck_provider_operations_safe_error_code"),
        ),
        sa.CheckConstraint(
            "state IN ('STARTED', 'SUCCEEDED', 'FAILED')", name=op.f("ck_provider_operations_state")
        ),
        sa.CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name=op.f("ck_provider_operations_trace_id"),
        ),
        sa.CheckConstraint(
            "(input_units IS NULL OR input_units >= 0) "
            "AND (output_units IS NULL OR output_units >= 0) "
            "AND (duration_ms IS NULL OR duration_ms >= 0)",
            name=op.f("ck_provider_operations_non_negative_measurements"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_provider_operations_ordered_completion_time"),
        ),
        sa.CheckConstraint(
            "reserved_cost_micro_usd >= 0 "
            "AND (actual_cost_micro_usd IS NULL OR actual_cost_micro_usd >= 0) "
            "AND (actual_cost_micro_usd IS NULL "
            "OR actual_cost_micro_usd <= reserved_cost_micro_usd)",
            name=op.f("ck_provider_operations_bounded_cost"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "processing_run_id"],
            ["processing_runs.workspace_id", "processing_runs.id"],
            name=op.f("fk_provider_operations_workspace_id_processing_run_id_processing_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "research_request_id"],
            ["research_requests.workspace_id", "research_requests.id"],
            name=op.f("fk_provider_operations_workspace_id_research_request_id_research_requests"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_provider_operations_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_operations")),
        sa.UniqueConstraint(
            "workspace_id", "id", name=op.f("uq_provider_operations_workspace_id_id")
        ),
    )
    op.create_index(
        op.f("ix_provider_operations_workspace_id_correlation_id_started_at"),
        "provider_operations",
        ["workspace_id", "correlation_id", "started_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_provider_operations_workspace_id_started_at"),
        "provider_operations",
        ["workspace_id", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the provider-operation ledger."""

    op.drop_index(
        op.f("ix_provider_operations_workspace_id_started_at"), table_name="provider_operations"
    )
    op.drop_index(
        op.f("ix_provider_operations_workspace_id_correlation_id_started_at"),
        table_name="provider_operations",
    )
    op.drop_table("provider_operations")
