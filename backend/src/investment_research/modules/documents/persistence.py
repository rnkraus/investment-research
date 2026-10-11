"""SQLAlchemy mappings and scoped repository for archive documents."""

from datetime import datetime
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    Select,
    String,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from investment_research.infrastructure.database.base import Base
from investment_research.modules.documents.contracts import (
    DocumentListCursor,
    DocumentListEntry,
    DocumentListPage,
    DocumentListQuery,
)
from investment_research.modules.documents.domain import (
    Document,
    DocumentLifecycleState,
    DocumentRelationshipError,
    DocumentScopeError,
    DocumentVersion,
    OriginalArtifact,
    OriginalMediaType,
)
from investment_research.modules.identity.domain import WorkspaceContext


class DocumentRow(Base):
    """Persisted logical archive item and current-version pointer."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "id", "current_version_id"],
            [
                "document_versions.workspace_id",
                "document_versions.document_id",
                "document_versions.id",
            ],
            name="fk_documents_current_version",
            ondelete="RESTRICT",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "document_type ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'",
            name="valid_document_type",
        ),
        CheckConstraint("lifecycle_state IN ('ACTIVE')", name="document_lifecycle_state"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_state: Mapped[DocumentLifecycleState] = mapped_column(
        SqlEnum(
            DocumentLifecycleState,
            name="document_lifecycle_state",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        ),
        nullable=False,
    )
    current_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    @classmethod
    def from_domain(cls, document: Document) -> Self:
        """Translate an immutable document into a pending database row."""

        return cls(
            id=document.id,
            workspace_id=document.workspace_id,
            document_type=document.document_type,
            lifecycle_state=document.lifecycle_state,
            current_version_id=document.current_version_id,
            created_at=document.created_at,
        )

    def to_domain(self) -> Document:
        """Translate a loaded database row into an immutable document."""

        return Document(
            id=self.id,
            workspace_id=self.workspace_id,
            document_type=self.document_type,
            lifecycle_state=self.lifecycle_state,
            current_version_id=self.current_version_id,
            created_at=self.created_at,
        )


class DocumentVersionRow(Base):
    """Persisted immutable content version and publication metadata."""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "document_id", "id"),
        UniqueConstraint("workspace_id", "document_id", "version_number"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "document_id"],
            ["documents.workspace_id", "documents.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("version_number > 0", name="positive_version_number"),
        CheckConstraint("char_length(btrim(title)) > 0", name="non_empty_title"),
        CheckConstraint(
            "language ~ '^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$'",
            name="valid_language",
        ),
        CheckConstraint("content_sha256 ~ '^[a-f0-9]{64}$'", name="valid_content_sha256"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    language: Mapped[str] = mapped_column(String(35), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @classmethod
    def from_domain(cls, version: DocumentVersion) -> Self:
        """Translate an immutable document version into a pending database row."""

        return cls(
            id=version.id,
            workspace_id=version.workspace_id,
            document_id=version.document_id,
            version_number=version.version_number,
            title=version.title,
            language=version.language,
            content_sha256=version.content_sha256,
            published_at=version.published_at,
            available_at=version.available_at,
            captured_at=version.captured_at,
        )

    def to_domain(self) -> DocumentVersion:
        """Translate a loaded database row into an immutable document version."""

        return DocumentVersion(
            id=self.id,
            workspace_id=self.workspace_id,
            document_id=self.document_id,
            version_number=self.version_number,
            title=self.title,
            language=self.language,
            content_sha256=self.content_sha256,
            published_at=self.published_at,
            available_at=self.available_at,
            captured_at=self.captured_at,
        )


class OriginalArtifactRow(Base):
    """Persisted immutable metadata for one captured source artifact."""

    __tablename__ = "original_artifacts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "document_version_id"),
        UniqueConstraint("workspace_id", "storage_key"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "document_version_id"],
            ["document_versions.workspace_id", "document_versions.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("char_length(btrim(storage_key)) > 0", name="non_empty_storage_key"),
        CheckConstraint("byte_size > 0", name="positive_byte_size"),
        CheckConstraint("checksum_sha256 ~ '^[a-f0-9]{64}$'", name="valid_checksum_sha256"),
        CheckConstraint(
            "media_type IN ('application/pdf', 'text/plain')",
            name="original_media_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[OriginalMediaType] = mapped_column(
        SqlEnum(
            OriginalMediaType,
            name="original_media_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=64,
        ),
        nullable=False,
    )
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @classmethod
    def from_domain(cls, original: OriginalArtifact) -> Self:
        """Translate immutable original metadata into a pending database row."""

        return cls(
            id=original.id,
            workspace_id=original.workspace_id,
            document_version_id=original.document_version_id,
            storage_key=original.storage_key,
            media_type=original.media_type,
            byte_size=original.byte_size,
            checksum_sha256=original.checksum_sha256,
            captured_at=original.captured_at,
        )

    def to_domain(self) -> OriginalArtifact:
        """Translate a loaded database row into immutable original metadata."""

        return OriginalArtifact(
            id=self.id,
            workspace_id=self.workspace_id,
            document_version_id=self.document_version_id,
            storage_key=self.storage_key,
            media_type=self.media_type,
            byte_size=self.byte_size,
            checksum_sha256=self.checksum_sha256,
            captured_at=self.captured_at,
        )


class SqlAlchemyDocumentRepository:
    """Document repository bound to one caller-owned SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_current_documents(
        self,
        context: WorkspaceContext,
        query: DocumentListQuery,
    ) -> DocumentListPage:
        """Read listing metadata through active current workspace relationships."""

        statement = (
            select(
                DocumentRow.id.label("document_id"),
                DocumentVersionRow.id.label("document_version_id"),
                DocumentVersionRow.title,
                DocumentVersionRow.language,
                DocumentVersionRow.published_at,
                DocumentVersionRow.available_at,
                DocumentVersionRow.captured_at,
                DocumentRow.created_at,
                OriginalArtifactRow.media_type,
                OriginalArtifactRow.byte_size,
            )
            .select_from(DocumentRow)
            .join(
                DocumentVersionRow,
                and_(
                    DocumentVersionRow.workspace_id == DocumentRow.workspace_id,
                    DocumentVersionRow.document_id == DocumentRow.id,
                    DocumentVersionRow.id == DocumentRow.current_version_id,
                ),
            )
            .join(
                OriginalArtifactRow,
                and_(
                    OriginalArtifactRow.workspace_id == DocumentVersionRow.workspace_id,
                    OriginalArtifactRow.document_version_id == DocumentVersionRow.id,
                ),
            )
            .where(
                DocumentRow.workspace_id == context.workspace_id,
                DocumentVersionRow.workspace_id == context.workspace_id,
                OriginalArtifactRow.workspace_id == context.workspace_id,
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
            )
            .order_by(DocumentRow.created_at.desc(), DocumentRow.id.desc())
            .limit(query.page_size + 1)
        )
        if query.after is not None:
            statement = statement.where(
                or_(
                    DocumentRow.created_at < query.after.created_at,
                    and_(
                        DocumentRow.created_at == query.after.created_at,
                        DocumentRow.id < query.after.document_id,
                    ),
                )
            )

        rows = self._session.execute(statement).mappings().all()
        items = tuple(DocumentListEntry.model_validate(row) for row in rows[: query.page_size])
        next_cursor = None
        if len(rows) > query.page_size:
            last = items[-1]
            next_cursor = DocumentListCursor(
                created_at=last.created_at,
                document_id=last.document_id,
            )
        return DocumentListPage(items=items, next_cursor=next_cursor)

    def add_capture(
        self,
        context: WorkspaceContext,
        document: Document,
        version: DocumentVersion,
        original: OriginalArtifact,
    ) -> None:
        """Stage one internally consistent capture inside the active workspace."""

        if any(
            workspace_id != context.workspace_id
            for workspace_id in (document.workspace_id, version.workspace_id, original.workspace_id)
        ):
            raise DocumentScopeError("document capture is outside the active workspace")
        if (
            document.current_version_id != version.id
            or version.document_id != document.id
            or original.document_version_id != version.id
        ):
            raise DocumentRelationshipError("document capture relationships are inconsistent")

        self._session.add_all(
            [
                DocumentRow.from_domain(document),
                DocumentVersionRow.from_domain(version),
                OriginalArtifactRow.from_domain(original),
            ]
        )

    def get_document_by_id(
        self,
        context: WorkspaceContext,
        document_id: UUID,
    ) -> Document | None:
        """Return an active document from the trusted workspace only."""

        row = self._session.scalar(
            select(DocumentRow).where(
                DocumentRow.workspace_id == context.workspace_id,
                DocumentRow.id == document_id,
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
            )
        )
        return None if row is None else row.to_domain()

    def get_current_version_by_id(
        self,
        context: WorkspaceContext,
        document_id: UUID,
        version_id: UUID,
    ) -> DocumentVersion | None:
        """Return only the requested document's active current version."""

        row = self._session.scalar(
            select(DocumentVersionRow)
            .join(
                DocumentRow,
                and_(
                    DocumentRow.workspace_id == DocumentVersionRow.workspace_id,
                    DocumentRow.id == DocumentVersionRow.document_id,
                    DocumentRow.current_version_id == DocumentVersionRow.id,
                ),
            )
            .where(
                DocumentVersionRow.workspace_id == context.workspace_id,
                DocumentVersionRow.document_id == document_id,
                DocumentVersionRow.id == version_id,
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
            )
        )
        return None if row is None else row.to_domain()

    def get_original_artifact(
        self,
        context: WorkspaceContext,
        document_id: UUID,
        version_id: UUID,
    ) -> OriginalArtifact | None:
        """Return original metadata only through an eligible current version."""

        row = self._session.scalar(
            self._eligible_original_statement(
                context,
                version_id,
                document_id=document_id,
            )
        )
        return None if row is None else row.to_domain()

    def get_original_artifact_by_version_id(
        self,
        context: WorkspaceContext,
        version_id: UUID,
    ) -> OriginalArtifact | None:
        """Resolve an original only through an eligible current version in scope."""

        row = self._session.scalar(self._eligible_original_statement(context, version_id))
        return None if row is None else row.to_domain()

    @staticmethod
    def _eligible_original_statement(
        context: WorkspaceContext,
        version_id: UUID,
        *,
        document_id: UUID | None = None,
    ) -> Select[tuple[OriginalArtifactRow]]:
        """Build the shared active, current, and workspace-scoped original query."""

        statement = (
            select(OriginalArtifactRow)
            .join(
                DocumentVersionRow,
                and_(
                    DocumentVersionRow.workspace_id == OriginalArtifactRow.workspace_id,
                    DocumentVersionRow.id == OriginalArtifactRow.document_version_id,
                ),
            )
            .join(
                DocumentRow,
                and_(
                    DocumentRow.workspace_id == DocumentVersionRow.workspace_id,
                    DocumentRow.id == DocumentVersionRow.document_id,
                    DocumentRow.current_version_id == DocumentVersionRow.id,
                ),
            )
            .where(
                OriginalArtifactRow.workspace_id == context.workspace_id,
                DocumentVersionRow.id == version_id,
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
            )
        )
        if document_id is not None:
            statement = statement.where(DocumentVersionRow.document_id == document_id)
        return statement
