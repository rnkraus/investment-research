"""SQLAlchemy mappings and scoped repository for document processing."""

from collections.abc import Sequence
from datetime import datetime
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    select,
    text,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from investment_research.infrastructure.database.base import Base
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import (
    M0_DISABLED_DENSE_INDEX_CONFIG_VERSION,
    M0_DISABLED_EMBEDDING_CONFIG_VERSION,
    Chunk,
    ChunkSet,
    ProcessingRelationshipError,
    ProcessingRun,
    ProcessingScopeError,
    ProcessingState,
    StructureBlock,
    StructureBlockType,
)


class ProcessingRunRow(Base):
    """Persisted processing intent and lifecycle state."""

    __tablename__ = "processing_runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "document_version_id", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "document_version_id"],
            ["document_versions.workspace_id", "document_versions.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "char_length(btrim(parser_version)) > 0 "
            "AND char_length(btrim(parser_config_version)) > 0 "
            "AND char_length(btrim(chunker_version)) > 0 "
            "AND char_length(btrim(chunking_config_version)) > 0 "
            "AND char_length(btrim(lexical_config_version)) > 0 "
            "AND char_length(btrim(embedding_config_version)) > 0 "
            "AND char_length(btrim(dense_index_config_version)) > 0",
            name="non_empty_configuration_versions",
        ),
        CheckConstraint(
            "(embedding_config_version = "
            f"'{M0_DISABLED_EMBEDDING_CONFIG_VERSION}' AND "
            "dense_index_config_version = "
            f"'{M0_DISABLED_DENSE_INDEX_CONFIG_VERSION}') OR "
            "(embedding_config_version <> "
            f"'{M0_DISABLED_EMBEDDING_CONFIG_VERSION}' AND "
            "dense_index_config_version <> "
            f"'{M0_DISABLED_DENSE_INDEX_CONFIG_VERSION}')",
            name="coherent_dense_index_configuration",
        ),
        CheckConstraint(
            "started_at IS NULL OR started_at >= created_at",
            name="ordered_start_time",
        ),
        CheckConstraint(
            "completed_at IS NULL OR (started_at IS NOT NULL AND completed_at >= started_at)",
            name="ordered_completion_time",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR "
            "(started_at IS NOT NULL AND lease_expires_at > started_at)",
            name="ordered_lease_expiry",
        ),
        CheckConstraint("attempt_count >= 0", name="non_negative_attempt_count"),
        CheckConstraint(
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
            "AND embedding_config_version <> "
            f"'{M0_DISABLED_EMBEDDING_CONFIG_VERSION}' AND dense_index_config_version <> "
            f"'{M0_DISABLED_DENSE_INDEX_CONFIG_VERSION}'))) OR "
            "(state = 'INDEXING' AND started_at IS NOT NULL AND completed_at IS NULL "
            "AND error_summary IS NULL AND attempt_count > 0 "
            "AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND embedding_config_version <> "
            f"'{M0_DISABLED_EMBEDDING_CONFIG_VERSION}' AND dense_index_config_version <> "
            f"'{M0_DISABLED_DENSE_INDEX_CONFIG_VERSION}') OR "
            "(state = 'SEARCHABLE' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND error_summary IS NULL AND attempt_count > 0 "
            "AND lease_id IS NULL AND lease_expires_at IS NULL "
            "AND embedding_config_version <> "
            f"'{M0_DISABLED_EMBEDDING_CONFIG_VERSION}' AND dense_index_config_version <> "
            f"'{M0_DISABLED_DENSE_INDEX_CONFIG_VERSION}') OR "
            "(state = 'FAILED' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND char_length(btrim(error_summary)) > 0 AND attempt_count > 0 "
            "AND lease_id IS NULL AND lease_expires_at IS NULL)",
            name="consistent_state",
        ),
        CheckConstraint(
            "state IN ('CAPTURED', 'PARSING', 'LEXICAL_READY', 'INDEXING', 'SEARCHABLE', 'FAILED')",
            name="processing_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    state: Mapped[ProcessingState] = mapped_column(
        SqlEnum(
            ProcessingState,
            name="processing_state",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        ),
        nullable=False,
    )
    parser_version: Mapped[str] = mapped_column(String(128), nullable=False)
    parser_config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    chunker_version: Mapped[str] = mapped_column(String(128), nullable=False)
    chunking_config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    lexical_config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    dense_index_config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    lease_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(String(500))

    @classmethod
    def from_domain(cls, run: ProcessingRun) -> Self:
        """Translate a processing run snapshot into a pending database row."""

        return cls(
            id=run.id,
            workspace_id=run.workspace_id,
            document_version_id=run.document_version_id,
            state=run.state,
            parser_version=run.parser_version,
            parser_config_version=run.parser_config_version,
            chunker_version=run.chunker_version,
            chunking_config_version=run.chunking_config_version,
            lexical_config_version=run.lexical_config_version,
            embedding_config_version=run.embedding_config_version,
            dense_index_config_version=run.dense_index_config_version,
            created_at=run.created_at,
            attempt_count=run.attempt_count,
            lease_id=run.lease_id,
            lease_expires_at=run.lease_expires_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            error_summary=run.error_summary,
        )

    def apply_state(self, run: ProcessingRun) -> None:
        """Apply mutable lifecycle fields after immutable identity checks."""

        self.state = run.state
        self.attempt_count = run.attempt_count
        self.lease_id = run.lease_id
        self.lease_expires_at = run.lease_expires_at
        self.started_at = run.started_at
        self.completed_at = run.completed_at
        self.error_summary = run.error_summary

    def to_domain(self) -> ProcessingRun:
        """Translate a loaded row into a validated processing run snapshot."""

        return ProcessingRun(
            id=self.id,
            workspace_id=self.workspace_id,
            document_version_id=self.document_version_id,
            state=self.state,
            parser_version=self.parser_version,
            parser_config_version=self.parser_config_version,
            chunker_version=self.chunker_version,
            chunking_config_version=self.chunking_config_version,
            lexical_config_version=self.lexical_config_version,
            embedding_config_version=self.embedding_config_version,
            dense_index_config_version=self.dense_index_config_version,
            created_at=self.created_at,
            attempt_count=self.attempt_count,
            lease_id=self.lease_id,
            lease_expires_at=self.lease_expires_at,
            started_at=self.started_at,
            completed_at=self.completed_at,
            error_summary=self.error_summary,
        )


class StructureBlockRow(Base):
    """Persisted canonical parsed block anchored to source offsets."""

    __tablename__ = "structure_blocks"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "document_version_id", "id"),
        UniqueConstraint("workspace_id", "processing_run_id", "document_version_id", "id"),
        UniqueConstraint("workspace_id", "processing_run_id", "ordinal"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "document_version_id"],
            ["document_versions.workspace_id", "document_versions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "document_version_id", "processing_run_id"],
            [
                "processing_runs.workspace_id",
                "processing_runs.document_version_id",
                "processing_runs.id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(source_locator_kind = 'PDF_PAGE' AND page_number > 0) OR "
            "(source_locator_kind = 'TEXT_PASSAGE' AND page_number IS NULL)",
            name="valid_source_locator",
        ),
        CheckConstraint("ordinal >= 0", name="non_negative_ordinal"),
        CheckConstraint("text ~ '[^[:space:]]'", name="non_empty_text"),
        CheckConstraint(
            "start_character >= 0 AND end_character > start_character",
            name="ordered_source_offsets",
        ),
        CheckConstraint(
            "char_length(text) = end_character - start_character",
            name="matching_text_length",
        ),
        CheckConstraint(
            "block_type IN ('HEADING', 'PARAGRAPH', 'LIST_ITEM', 'TABLE')",
            name="structure_block_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source_locator_kind: Mapped[SourceLocatorKind] = mapped_column(
        SqlEnum(
            SourceLocatorKind,
            name="source_locator_kind",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        ),
        nullable=False,
    )
    page_number: Mapped[int | None] = mapped_column(Integer)
    block_type: Mapped[StructureBlockType] = mapped_column(
        SqlEnum(
            StructureBlockType,
            name="structure_block_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        ),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_character: Mapped[int] = mapped_column(Integer, nullable=False)
    end_character: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @classmethod
    def from_domain(cls, block: StructureBlock) -> Self:
        """Translate an immutable structure block into a pending row."""

        return cls(**block.model_dump())


class ChunkSetRow(Base):
    """Persisted versioned chunking result and active-profile marker."""

    __tablename__ = "chunk_sets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "processing_run_id", "document_version_id", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "document_version_id"],
            ["document_versions.workspace_id", "document_versions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "document_version_id", "processing_run_id"],
            [
                "processing_runs.workspace_id",
                "processing_runs.document_version_id",
                "processing_runs.id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "char_length(btrim(chunker_version)) > 0 "
            "AND char_length(btrim(chunking_config_version)) > 0 "
            "AND char_length(btrim(lexical_config_version)) > 0",
            name="non_empty_configuration_versions",
        ),
        Index(
            "ux_chunk_sets_active_profile",
            "workspace_id",
            "document_version_id",
            "lexical_config_version",
            unique=True,
            postgresql_where=text("is_active IS TRUE"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    chunker_version: Mapped[str] = mapped_column(String(128), nullable=False)
    chunking_config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    lexical_config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @classmethod
    def from_domain(cls, chunk_set: ChunkSet) -> Self:
        """Translate an immutable chunk set into a pending row."""

        return cls(**chunk_set.model_dump())


class ChunkRow(Base):
    """Persisted retrieval chunk with a generated English search vector."""

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "chunk_set_id", "ordinal"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "document_version_id"],
            ["document_versions.workspace_id", "document_versions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "processing_run_id", "document_version_id", "chunk_set_id"],
            [
                "chunk_sets.workspace_id",
                "chunk_sets.processing_run_id",
                "chunk_sets.document_version_id",
                "chunk_sets.id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "processing_run_id", "document_version_id", "structure_block_id"],
            [
                "structure_blocks.workspace_id",
                "structure_blocks.processing_run_id",
                "structure_blocks.document_version_id",
                "structure_blocks.id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="non_negative_ordinal"),
        CheckConstraint("text ~ '[^[:space:]]'", name="non_empty_text"),
        CheckConstraint("token_count > 0", name="positive_token_count"),
        Index("ix_chunks_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    chunk_set_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    structure_block_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english'::regconfig, text)", persisted=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @classmethod
    def from_domain(cls, chunk: Chunk) -> Self:
        """Translate an immutable chunk into a row without writing its generated vector."""

        return cls(**chunk.model_dump())

    def to_domain(self) -> Chunk:
        """Translate a loaded row into one immutable canonical chunk."""

        return Chunk(
            id=self.id,
            workspace_id=self.workspace_id,
            processing_run_id=self.processing_run_id,
            document_version_id=self.document_version_id,
            chunk_set_id=self.chunk_set_id,
            structure_block_id=self.structure_block_id,
            ordinal=self.ordinal,
            text=self.text,
            token_count=self.token_count,
            created_at=self.created_at,
        )


class SqlAlchemyProcessingRepository:
    """Processing repository bound to one caller-owned SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_run(self, context: WorkspaceContext, run: ProcessingRun) -> None:
        """Stage one processing intent inside the active workspace."""

        self._require_scope(context, run.workspace_id)
        self._session.add(ProcessingRunRow.from_domain(run))

    def get_run_by_id(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> ProcessingRun | None:
        """Return a processing run from the trusted workspace only."""

        row = self._session.scalar(
            select(ProcessingRunRow).where(
                ProcessingRunRow.workspace_id == context.workspace_id,
                ProcessingRunRow.id == processing_run_id,
            )
        )
        return None if row is None else row.to_domain()

    def get_run_by_id_for_update(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> ProcessingRun | None:
        """Lock one scoped run until the caller's transaction completes."""

        row = self._session.scalar(
            select(ProcessingRunRow)
            .where(
                ProcessingRunRow.workspace_id == context.workspace_id,
                ProcessingRunRow.id == processing_run_id,
            )
            .with_for_update()
        )
        return None if row is None else row.to_domain()

    def update_run(self, context: WorkspaceContext, run: ProcessingRun) -> bool:
        """Update lifecycle fields without changing immutable run identity or configuration."""

        self._require_scope(context, run.workspace_id)
        row = self._session.scalar(
            select(ProcessingRunRow).where(
                ProcessingRunRow.workspace_id == context.workspace_id,
                ProcessingRunRow.id == run.id,
            )
        )
        if row is None:
            return False
        if (
            row.document_version_id != run.document_version_id
            or row.parser_version != run.parser_version
            or row.parser_config_version != run.parser_config_version
            or row.chunker_version != run.chunker_version
            or row.chunking_config_version != run.chunking_config_version
            or row.lexical_config_version != run.lexical_config_version
            or row.embedding_config_version != run.embedding_config_version
            or row.dense_index_config_version != run.dense_index_config_version
        ):
            raise ProcessingRelationshipError("processing run identity or configuration changed")

        row.apply_state(run)
        return True

    def list_active_chunks(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> Sequence[Chunk]:
        """Return active canonical chunks for one scoped run in stable order."""

        rows = self._session.scalars(
            select(ChunkRow)
            .join(
                ChunkSetRow,
                and_(
                    ChunkSetRow.workspace_id == ChunkRow.workspace_id,
                    ChunkSetRow.processing_run_id == ChunkRow.processing_run_id,
                    ChunkSetRow.document_version_id == ChunkRow.document_version_id,
                    ChunkSetRow.id == ChunkRow.chunk_set_id,
                ),
            )
            .join(
                ProcessingRunRow,
                and_(
                    ProcessingRunRow.workspace_id == ChunkRow.workspace_id,
                    ProcessingRunRow.id == ChunkRow.processing_run_id,
                    ProcessingRunRow.document_version_id == ChunkRow.document_version_id,
                ),
            )
            .where(
                ChunkRow.workspace_id == context.workspace_id,
                ChunkRow.processing_run_id == processing_run_id,
                ChunkSetRow.is_active.is_(True),
                ChunkSetRow.lexical_config_version == ProcessingRunRow.lexical_config_version,
            )
            .order_by(ChunkRow.ordinal, ChunkRow.id)
        ).all()
        return tuple(row.to_domain() for row in rows)

    def add_lexical_output(
        self,
        context: WorkspaceContext,
        blocks: Sequence[StructureBlock],
        chunk_set: ChunkSet,
        chunks: Sequence[Chunk],
    ) -> None:
        """Stage parsed blocks and chunks only when every relationship is consistent."""

        if not blocks or not chunks:
            raise ProcessingRelationshipError("lexical output requires blocks and chunks")

        workspace_ids = {
            chunk_set.workspace_id,
            *(block.workspace_id for block in blocks),
            *(chunk.workspace_id for chunk in chunks),
        }
        if workspace_ids != {context.workspace_id}:
            raise ProcessingScopeError("processing output is outside the active workspace")

        block_ids = {block.id for block in blocks}
        if len(block_ids) != len(blocks):
            raise ProcessingRelationshipError("structure block identifiers must be unique")
        if any(
            block.processing_run_id != chunk_set.processing_run_id
            or block.document_version_id != chunk_set.document_version_id
            for block in blocks
        ):
            raise ProcessingRelationshipError("structure blocks do not match the chunk set source")
        if any(
            chunk.processing_run_id != chunk_set.processing_run_id
            or chunk.document_version_id != chunk_set.document_version_id
            or chunk.chunk_set_id != chunk_set.id
            or chunk.structure_block_id not in block_ids
            for chunk in chunks
        ):
            raise ProcessingRelationshipError("chunks do not match the chunk set and source blocks")

        self._session.add_all(
            [
                *(StructureBlockRow.from_domain(block) for block in blocks),
                ChunkSetRow.from_domain(chunk_set),
            ]
        )
        self._session.flush()
        self._session.add_all([ChunkRow.from_domain(chunk) for chunk in chunks])

    @staticmethod
    def _require_scope(context: WorkspaceContext, workspace_id: UUID) -> None:
        """Reject a cross-workspace write before accessing persistence state."""

        if workspace_id != context.workspace_id:
            raise ProcessingScopeError("processing write is outside the active workspace")
