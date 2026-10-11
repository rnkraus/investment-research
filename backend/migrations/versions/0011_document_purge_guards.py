"""Guard permanent purges and reject writes against excluded evidence.

Revision ID: 0011_document_purge_guards
Revises: 0010_permanent_document_deletion
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_document_purge_guards"
down_revision: str | Sequence[str] | None = "0010_permanent_document_deletion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_TABLES = (
    "document_versions",
    "original_artifacts",
    "processing_runs",
    "structure_blocks",
    "chunk_sets",
    "chunks",
    "evidence_spans",
    "retrieval_candidates",
    "statement_evidence",
)
RESEARCH_TABLES = ("research_requests", "answers", "answer_statements")


def _current_version_constraint(ondelete: str) -> None:
    op.drop_constraint("fk_documents_current_version", "documents", type_="foreignkey")
    op.create_foreign_key(
        "fk_documents_current_version",
        "documents",
        "document_versions",
        ["workspace_id", "id", "current_version_id"],
        ["workspace_id", "document_id", "id"],
        ondelete=ondelete,
        deferrable=True,
        initially="DEFERRED",
    )


def upgrade() -> None:
    """Retain immutable updates and admit deletes only for recorded pending targets."""

    _current_version_constraint("NO ACTION")
    op.execute("""
        CREATE OR REPLACE FUNCTION reject_m0_0_immutable_row_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE target_document uuid;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF TG_TABLE_NAME = 'document_versions' THEN
                    target_document := OLD.document_id;
                ELSE
                    SELECT document_id INTO target_document FROM document_versions
                    WHERE workspace_id = OLD.workspace_id AND id = OLD.document_version_id;
                END IF;
                PERFORM d.id FROM documents d JOIN document_deletions r
                    ON r.workspace_id = d.workspace_id AND r.document_id = d.id
                WHERE d.workspace_id = OLD.workspace_id AND d.id = target_document
                    AND d.lifecycle_state = 'PURGE_PENDING' AND r.state = 'PENDING'
                FOR UPDATE OF d;
                IF FOUND THEN RETURN OLD; END IF;
            END IF;
            RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'original rows are immutable';
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION require_active_document(source_workspace uuid, source_document uuid)
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM id FROM documents
            WHERE workspace_id = source_workspace AND id = source_document
                AND lifecycle_state = 'ACTIVE' FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'source is unavailable';
            END IF;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION guard_document_source_write()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE source_version uuid; source_document uuid;
        BEGIN
            IF TG_TABLE_NAME = 'document_versions' THEN
                source_document := NEW.document_id;
            ELSE
                IF TG_TABLE_NAME = 'retrieval_candidates' THEN
                    SELECT document_version_id INTO source_version FROM chunks
                    WHERE workspace_id = NEW.workspace_id AND id = NEW.chunk_id;
                ELSIF TG_TABLE_NAME = 'statement_evidence' THEN
                    SELECT document_version_id INTO source_version FROM evidence_spans
                    WHERE workspace_id = NEW.workspace_id AND id = NEW.evidence_span_id;
                ELSE
                    source_version := NEW.document_version_id;
                END IF;
                SELECT document_id INTO source_document FROM document_versions
                WHERE workspace_id = NEW.workspace_id AND id = source_version;
            END IF;
            -- Missing relationships remain the existing foreign keys' responsibility.
            IF source_document IS NOT NULL THEN
                PERFORM require_active_document(NEW.workspace_id, source_document);
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION guard_research_source_write()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE request_id uuid; target_answer_id uuid; target uuid; filter_ids uuid[];
        BEGIN
            IF TG_TABLE_NAME = 'research_requests' THEN
                request_id := NEW.id;
                filter_ids := NEW.document_ids;
            ELSE
                IF TG_TABLE_NAME = 'answers' THEN
                    request_id := NEW.research_request_id;
                    target_answer_id := NEW.id;
                ELSE
                    target_answer_id := NEW.answer_id;
                    SELECT research_request_id INTO request_id FROM answers
                    WHERE workspace_id = NEW.workspace_id AND id = target_answer_id;
                END IF;
                SELECT document_ids INTO filter_ids FROM research_requests
                WHERE workspace_id = NEW.workspace_id AND id = request_id;
            END IF;
            FOR target IN
                SELECT DISTINCT candidate FROM (
                    SELECT unnest(filter_ids) AS candidate
                    UNION
                    SELECT v.document_id FROM retrieval_runs r
                    JOIN retrieval_candidates c ON c.workspace_id = r.workspace_id
                        AND c.retrieval_run_id = r.id
                    JOIN chunks ch ON ch.workspace_id = c.workspace_id AND ch.id = c.chunk_id
                    JOIN document_versions v ON v.workspace_id = ch.workspace_id
                        AND v.id = ch.document_version_id
                    WHERE r.workspace_id = NEW.workspace_id AND r.research_request_id = request_id
                    UNION
                    SELECT v.document_id FROM answer_statements s
                    JOIN statement_evidence e ON e.workspace_id = s.workspace_id
                        AND e.answer_statement_id = s.id
                    JOIN evidence_spans sp ON sp.workspace_id = e.workspace_id
                        AND sp.id = e.evidence_span_id
                    JOIN document_versions v ON v.workspace_id = sp.workspace_id
                        AND v.id = sp.document_version_id
                    WHERE s.workspace_id = NEW.workspace_id AND s.answer_id = target_answer_id
                ) sources ORDER BY candidate
            LOOP
                -- Preserve the existing identical handling of unknown and foreign filters.
                IF EXISTS (SELECT 1 FROM documents
                           WHERE workspace_id = NEW.workspace_id AND id = target)
                    OR EXISTS (SELECT 1 FROM document_deletions
                               WHERE workspace_id = NEW.workspace_id AND document_id = target) THEN
                    PERFORM require_active_document(NEW.workspace_id, target);
                END IF;
            END LOOP;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION guard_pending_document_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.lifecycle_state = 'PURGE_PENDING' AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'document deletion is irreversible';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_pending_document_change BEFORE UPDATE ON documents
        FOR EACH ROW EXECUTE FUNCTION guard_pending_document_change();
    """)
    for table_name in SOURCE_TABLES:
        events = (
            "INSERT"
            if table_name in {"document_versions", "original_artifacts"}
            else "INSERT OR UPDATE"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table_name}_active_source BEFORE {events} ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION guard_document_source_write()"
        )
    for table_name in RESEARCH_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table_name}_active_source "
            f"BEFORE INSERT OR UPDATE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION guard_research_source_write()"
        )


def downgrade() -> None:
    """Restore the prior immutable boundary without deleting canonical records."""

    for table_name in (*SOURCE_TABLES, *RESEARCH_TABLES):
        op.execute(f"DROP TRIGGER trg_{table_name}_active_source ON {table_name}")
    op.execute("DROP TRIGGER trg_pending_document_change ON documents")
    op.execute("DROP FUNCTION guard_pending_document_change()")
    op.execute("DROP FUNCTION guard_research_source_write()")
    op.execute("DROP FUNCTION guard_document_source_write()")
    op.execute("DROP FUNCTION require_active_document(uuid, uuid)")
    op.execute("""
        CREATE OR REPLACE FUNCTION reject_m0_0_immutable_row_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'original rows are immutable';
        END $$;
    """)
    _current_version_constraint("RESTRICT")
