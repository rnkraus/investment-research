"""SQLAlchemy mappings and scoped repository for reproducible retrieval."""

from collections.abc import Sequence
from datetime import datetime
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from investment_research.infrastructure.database.base import Base
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.domain import (
    RetrievalCandidate,
    RetrievalFilters,
    RetrievalMode,
    RetrievalOrigin,
    RetrievalRun,
    SearchRelationshipError,
    SearchScopeError,
)


class RetrievalRunRow(Base):
    """Persisted query, filters, configuration, and timing for one retrieval."""

    __tablename__ = "retrieval_runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "research_request_id", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "research_request_id"],
            ["research_requests.workspace_id", "research_requests.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("query_text ~ '[^[:space:]]'", name="non_empty_query_text"),
        CheckConstraint(
            "char_length(btrim(normalized_query)) > 0",
            name="non_empty_normalized_query",
        ),
        CheckConstraint(
            "char_length(btrim(lexical_config_version)) > 0",
            name="non_empty_lexical_config_version",
        ),
        CheckConstraint(
            "embedding_config_version IS NULL OR char_length(btrim(embedding_config_version)) > 0",
            name="non_empty_embedding_config_version",
        ),
        CheckConstraint(
            "dense_index_config_version IS NULL "
            "OR char_length(btrim(dense_index_config_version)) > 0",
            name="non_empty_dense_index_config_version",
        ),
        CheckConstraint(
            "char_length(btrim(selector_config_version)) > 0",
            name="non_empty_selector_config_version",
        ),
        CheckConstraint(
            "lexical_candidate_limit BETWEEN 1 AND 100",
            name="bounded_lexical_candidate_limit",
        ),
        CheckConstraint(
            "dense_candidate_limit BETWEEN 0 AND 100",
            name="bounded_dense_candidate_limit",
        ),
        CheckConstraint(
            "context_chunk_limit IS NULL OR context_chunk_limit BETWEEN 1 AND 8",
            name="bounded_context_chunk_limit",
        ),
        CheckConstraint(
            "context_character_limit IS NULL OR context_character_limit BETWEEN 1 AND 24000",
            name="bounded_context_character_limit",
        ),
        CheckConstraint("mode IN ('LEXICAL', 'DUAL_LANE')", name="retrieval_mode"),
        CheckConstraint(
            "(mode = 'LEXICAL' AND embedding_config_version IS NULL "
            "AND dense_index_config_version IS NULL AND dense_candidate_limit = 0 "
            "AND context_chunk_limit IS NULL AND context_character_limit IS NULL) OR "
            "(mode = 'DUAL_LANE' AND embedding_config_version IS NOT NULL "
            "AND dense_index_config_version IS NOT NULL AND dense_candidate_limit > 0 "
            "AND context_chunk_limit IS NOT NULL AND context_character_limit IS NOT NULL)",
            name="coherent_retrieval_mode",
        ),
        CheckConstraint("completed_at >= started_at", name="ordered_run_times"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    research_request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_query: Mapped[str] = mapped_column(Text, nullable=False)
    filter_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    mode: Mapped[RetrievalMode] = mapped_column(
        SqlEnum(
            RetrievalMode,
            name="retrieval_mode",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        ),
        nullable=False,
    )
    lexical_config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_config_version: Mapped[str | None] = mapped_column(String(128))
    dense_index_config_version: Mapped[str | None] = mapped_column(String(128))
    selector_config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    lexical_candidate_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    dense_candidate_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    context_chunk_limit: Mapped[int | None] = mapped_column(Integer)
    context_character_limit: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @classmethod
    def from_domain(cls, run: RetrievalRun) -> Self:
        """Translate a completed retrieval run into a pending database row."""

        return cls(
            id=run.id,
            workspace_id=run.workspace_id,
            research_request_id=run.research_request_id,
            query_text=run.query_text,
            normalized_query=run.normalized_query,
            filter_snapshot=run.filters.model_dump(mode="json"),
            mode=run.mode,
            lexical_config_version=run.lexical_config_version,
            embedding_config_version=run.embedding_config_version,
            dense_index_config_version=run.dense_index_config_version,
            selector_config_version=run.selector_config_version,
            lexical_candidate_limit=run.lexical_candidate_limit,
            dense_candidate_limit=run.dense_candidate_limit,
            context_chunk_limit=run.context_chunk_limit,
            context_character_limit=run.context_character_limit,
            started_at=run.started_at,
            completed_at=run.completed_at,
        )

    def to_domain(self) -> RetrievalRun:
        """Translate a loaded row into a validated retrieval run snapshot."""

        return RetrievalRun(
            id=self.id,
            workspace_id=self.workspace_id,
            research_request_id=self.research_request_id,
            query_text=self.query_text,
            normalized_query=self.normalized_query,
            filters=RetrievalFilters.model_validate(self.filter_snapshot),
            mode=self.mode,
            lexical_config_version=self.lexical_config_version,
            embedding_config_version=self.embedding_config_version,
            dense_index_config_version=self.dense_index_config_version,
            selector_config_version=self.selector_config_version,
            lexical_candidate_limit=self.lexical_candidate_limit,
            dense_candidate_limit=self.dense_candidate_limit,
            context_chunk_limit=self.context_chunk_limit,
            context_character_limit=self.context_character_limit,
            started_at=self.started_at,
            completed_at=self.completed_at,
        )


class RetrievalCandidateRow(Base):
    """Persisted ranked chunk selected by one retrieval run."""

    __tablename__ = "retrieval_candidates"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "retrieval_run_id", "id"),
        UniqueConstraint("workspace_id", "retrieval_run_id", "origin", "rank"),
        UniqueConstraint("workspace_id", "retrieval_run_id", "origin", "chunk_id"),
        UniqueConstraint("workspace_id", "retrieval_run_id", "selection_rank"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "retrieval_run_id"],
            ["retrieval_runs.workspace_id", "retrieval_runs.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "chunk_id"],
            ["chunks.workspace_id", "chunks.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("rank > 0", name="positive_rank"),
        CheckConstraint(
            "raw_score > '-Infinity'::double precision "
            "AND raw_score < 'Infinity'::double precision",
            name="finite_raw_score",
        ),
        CheckConstraint(
            "selection_rank IS NULL OR selection_rank > 0",
            name="positive_selection_rank",
        ),
        CheckConstraint(
            "fused_rank IS NULL OR fused_rank > 0",
            name="positive_fused_rank",
        ),
        CheckConstraint(
            "reranker_score IS NULL OR "
            "(reranker_score > '-Infinity'::double precision "
            "AND reranker_score < 'Infinity'::double precision)",
            name="finite_reranker_score",
        ),
        CheckConstraint(
            "origin IN ('LEXICAL', 'DENSE', 'STRUCTURED')",
            name="retrieval_origin",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    retrieval_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    origin: Mapped[RetrievalOrigin] = mapped_column(
        SqlEnum(
            RetrievalOrigin,
            name="retrieval_origin",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        ),
        nullable=False,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_score: Mapped[float] = mapped_column(Float, nullable=False)
    selection_rank: Mapped[int | None] = mapped_column(Integer)
    fused_rank: Mapped[int | None] = mapped_column(Integer)
    reranker_score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @classmethod
    def from_domain(cls, candidate: RetrievalCandidate) -> Self:
        """Translate an immutable retrieval candidate into a pending row."""

        return cls(**candidate.model_dump())

    def to_domain(self) -> RetrievalCandidate:
        """Translate a loaded row into a validated retrieval candidate snapshot."""

        return RetrievalCandidate(
            id=self.id,
            workspace_id=self.workspace_id,
            retrieval_run_id=self.retrieval_run_id,
            chunk_id=self.chunk_id,
            origin=self.origin,
            rank=self.rank,
            raw_score=self.raw_score,
            selection_rank=self.selection_rank,
            fused_rank=self.fused_rank,
            reranker_score=self.reranker_score,
            created_at=self.created_at,
        )


class SqlAlchemyRetrievalRepository:
    """Retrieval repository bound to one caller-owned SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_result(
        self,
        context: WorkspaceContext,
        run: RetrievalRun,
        candidates: Sequence[RetrievalCandidate],
    ) -> None:
        """Stage one retrieval result only when scope and ranks are consistent."""

        self._require_scope(context, run.workspace_id)
        ranks_by_origin: dict[RetrievalOrigin, list[int]] = {}
        chunks_by_origin: set[tuple[RetrievalOrigin, UUID]] = set()
        selection_ranks: list[int] = []
        selected_chunk_ids: set[UUID] = set()
        allowed_origins = {RetrievalOrigin.LEXICAL}
        limits = {RetrievalOrigin.LEXICAL: run.lexical_candidate_limit}
        if run.mode is RetrievalMode.DUAL_LANE:
            allowed_origins.add(RetrievalOrigin.DENSE)
            limits[RetrievalOrigin.DENSE] = run.dense_candidate_limit
        for candidate in candidates:
            self._require_scope(context, candidate.workspace_id)
            if candidate.retrieval_run_id != run.id:
                raise SearchRelationshipError("candidate does not belong to the retrieval run")
            if candidate.origin not in allowed_origins:
                raise SearchRelationshipError(
                    "candidate origin is not enabled for the retrieval run"
                )
            ranks_by_origin.setdefault(candidate.origin, []).append(candidate.rank)
            origin_chunk = (candidate.origin, candidate.chunk_id)
            if origin_chunk in chunks_by_origin:
                raise SearchRelationshipError("candidate chunk is duplicated within one origin")
            chunks_by_origin.add(origin_chunk)
            if candidate.selection_rank is not None:
                selection_ranks.append(candidate.selection_rank)
                if candidate.chunk_id in selected_chunk_ids:
                    raise SearchRelationshipError("selected candidate chunks must be unique")
                selected_chunk_ids.add(candidate.chunk_id)

        for origin, ranks in ranks_by_origin.items():
            if sorted(ranks) != list(range(1, len(ranks) + 1)):
                raise SearchRelationshipError("candidate ranks must be contiguous per origin")
            if len(ranks) > limits[origin]:
                raise SearchRelationshipError("candidate count exceeds its configured lane limit")
        if sorted(selection_ranks) != list(range(1, len(selection_ranks) + 1)):
            raise SearchRelationshipError("selection ranks must be contiguous")
        if run.context_chunk_limit is not None and len(selection_ranks) > run.context_chunk_limit:
            raise SearchRelationshipError("selected candidate count exceeds the context limit")

        self._session.add(RetrievalRunRow.from_domain(run))
        self._session.flush()
        self._session.add_all(
            RetrievalCandidateRow.from_domain(candidate) for candidate in candidates
        )

    def get_run_by_id(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
    ) -> RetrievalRun | None:
        """Return a retrieval run from the trusted workspace only."""

        row = self._session.scalar(
            select(RetrievalRunRow).where(
                RetrievalRunRow.workspace_id == context.workspace_id,
                RetrievalRunRow.id == retrieval_run_id,
            )
        )
        return None if row is None else row.to_domain()

    def list_candidates(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
    ) -> Sequence[RetrievalCandidate]:
        """Return scoped candidates in stable final-or-origin retrieval order."""

        run_exists = self._session.scalar(
            select(RetrievalRunRow.id).where(
                RetrievalRunRow.workspace_id == context.workspace_id,
                RetrievalRunRow.id == retrieval_run_id,
            )
        )
        if run_exists is None:
            return ()

        rows = self._session.scalars(
            select(RetrievalCandidateRow)
            .where(
                RetrievalCandidateRow.workspace_id == context.workspace_id,
                RetrievalCandidateRow.retrieval_run_id == retrieval_run_id,
            )
            .order_by(
                RetrievalCandidateRow.selection_rank.asc().nulls_last(),
                RetrievalCandidateRow.origin,
                RetrievalCandidateRow.rank,
                RetrievalCandidateRow.id,
            )
        )
        return tuple(row.to_domain() for row in rows)

    @staticmethod
    def _require_scope(context: WorkspaceContext, workspace_id: UUID) -> None:
        """Reject a cross-workspace write before accessing persistence state."""

        if workspace_id != context.workspace_id:
            raise SearchScopeError("retrieval write is outside the active workspace")
