"""Exercise irreversible cleanup using synthetic PostgreSQL and local files only."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, delete, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from investment_research.infrastructure.database.document_deletion import (
    PostgresDocumentDeletionRepository,
    create_document_deletion_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.modules.documents.deletion import (
    PurgeDocumentService,
    RequestDocumentDeletionService,
)
from investment_research.modules.documents.deletion_contracts import (
    DeleteDocumentsCommand,
    DocumentDeletionState,
)
from investment_research.modules.documents.deletion_ports import (
    DocumentDeletionBusyError,
    DocumentDeletionUnavailableError,
    DocumentPurgePendingError,
)
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import (
    DocumentDeletionRow,
    DocumentRow,
    DocumentVersionRow,
    OriginalArtifactRow,
    SqlAlchemyDocumentRepository,
)
from investment_research.modules.documents.ports import ObjectNotFoundError, ObjectStorageError
from investment_research.modules.evidence.domain import (
    AnswerValidation,
    EvidenceRelationship,
    EvidenceSpan,
    StatementEvidence,
    ValidationRule,
)
from investment_research.modules.evidence.persistence import (
    AnswerValidationRow,
    EvidenceSpanRow,
    SqlAlchemyEvidenceRepository,
    StatementEvidenceRow,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import (
    ProviderOperation,
    ProviderOperationKind,
    ProviderOperationState,
)
from investment_research.modules.operations.persistence import (
    SqlAlchemyProviderOperationRepository,
)
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import (
    ChunkRow,
    ChunkSetRow,
    ProcessingRunRow,
    SqlAlchemyProcessingRepository,
    StructureBlockRow,
)
from investment_research.modules.research.persistence import (
    AnswerRow,
    AnswerStatementRow,
    ResearchRequestRow,
    SqlAlchemyResearchRepository,
)
from investment_research.modules.search.application import dense_vector_id
from investment_research.modules.search.contracts import DenseVectorId
from investment_research.modules.search.persistence import RetrievalCandidateRow, RetrievalRunRow
from tests.integration.factories import (
    FIXTURE_TIME,
    ResearchSource,
    make_answer_result,
    make_research_source,
    make_retrieval_result,
    persist_answer_result,
    persist_research_source,
    persist_retrieval_result,
    persist_workspace,
)

PURGE_TIME = FIXTURE_TIME + timedelta(days=1)


def related_source(owner: ResearchSource) -> ResearchSource:
    """Create a second independent document in the same trusted workspace."""
    source = make_research_source(label="Retained same-workspace source")
    workspace_id = owner.context.workspace_id
    return replace(
        source,
        context=owner.context,
        workspace=owner.workspace,
        document=source.document.model_copy(update={"workspace_id": workspace_id}),
        version=source.version.model_copy(update={"workspace_id": workspace_id}),
        original=source.original.model_copy(
            update={
                "workspace_id": workspace_id,
                "storage_key": f"workspaces/{workspace_id}/originals/{source.original.id}.pdf",
            }
        ),
        processing_run=source.processing_run.model_copy(update={"workspace_id": workspace_id}),
        blocks=tuple(
            item.model_copy(update={"workspace_id": workspace_id}) for item in source.blocks
        ),
        chunk_set=source.chunk_set.model_copy(update={"workspace_id": workspace_id}),
        chunks=tuple(
            item.model_copy(update={"workspace_id": workspace_id}) for item in source.chunks
        ),
        request=source.request.model_copy(update={"workspace_id": workspace_id}),
    )


def persist_related_source(engine: Engine, source: ResearchSource) -> None:
    factory = create_session_factory(engine)
    with transactional_session(factory) as session:
        SqlAlchemyDocumentRepository(session).add_capture(
            source.context, source.document, source.version, source.original
        )
    with transactional_session(factory) as session:
        repository = SqlAlchemyProcessingRepository(session)
        repository.add_run(source.context, source.processing_run)
    with transactional_session(factory) as session:
        SqlAlchemyProcessingRepository(session).add_lexical_output(
            source.context, source.blocks, source.chunk_set, source.chunks
        )
    with transactional_session(factory) as session:
        SqlAlchemyResearchRepository(session).add_request(source.context, source.request)


def persist_answer_graph(engine: Engine, source: ResearchSource) -> UUID:
    retrieval = make_retrieval_result(source)
    persist_retrieval_result(engine, source, retrieval)
    answer = make_answer_result(source, retrieval)
    persist_answer_result(engine, source, answer)
    block = source.blocks[0]
    span = EvidenceSpan(
        id=uuid4(),
        workspace_id=source.context.workspace_id,
        document_version_id=source.version.id,
        structure_block_id=block.id,
        source_locator_kind=block.source_locator_kind,
        page_number=block.page_number,
        source_start_character=None,
        source_end_character=None,
        start_character=0,
        end_character=len(block.text),
        exact_text=block.text,
        text_sha256=sha256(block.text.encode()).hexdigest(),
        created_at=FIXTURE_TIME,
    )
    factory = create_session_factory(engine)
    with transactional_session(factory) as session:
        repository = SqlAlchemyEvidenceRepository(session)
        repository.add_span(source.context, span)
        session.flush()
        repository.add_statement_evidence(
            source.context,
            answer.statements[0].id,
            (
                StatementEvidence(
                    id=uuid4(),
                    workspace_id=source.context.workspace_id,
                    answer_statement_id=answer.statements[0].id,
                    evidence_span_id=span.id,
                    relationship=EvidenceRelationship.SUPPORTS,
                    display_order=0,
                    created_at=FIXTURE_TIME,
                ),
            ),
        )
        repository.add_answer_validations(
            source.context,
            answer.answer.id,
            (
                AnswerValidation(
                    id=uuid4(),
                    workspace_id=source.context.workspace_id,
                    answer_id=answer.answer.id,
                    rule=ValidationRule.COVERAGE,
                    passed=True,
                    validation_config_version="deletion-regression-v1",
                    validated_at=FIXTURE_TIME,
                ),
            ),
        )
    return answer.answer.id


@dataclass
class RecordingVectorPurger:
    calls: list[tuple[UUID, tuple[DenseVectorId, ...]]] = field(default_factory=list)

    def delete_vectors(
        self, context: WorkspaceContext, vector_ids: tuple[DenseVectorId, ...]
    ) -> None:
        self.calls.append((context.workspace_id, vector_ids))


def test_permanent_purge_removes_complete_graph_and_preserves_other_sources_and_budget(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    engine = integration_database_engine
    source = make_research_source(label="Permanent deletion")
    retained = related_source(source)
    foreign = make_research_source(label="Foreign retained source")
    persist_research_source(engine, source)
    persist_related_source(engine, retained)
    persist_research_source(engine, foreign)
    deleted_answer_id = persist_answer_graph(engine, source)
    retained_answer_id = persist_answer_graph(engine, retained)
    foreign_answer_id = persist_answer_graph(engine, foreign)
    factory = create_session_factory(engine)
    operation = ProviderOperation(
        id=uuid4(),
        workspace_id=source.context.workspace_id,
        correlation_id=uuid4(),
        research_request_id=source.request.id,
        operation=ProviderOperationKind.ANSWER_GENERATION,
        provider="openai",
        model="synthetic-model",
        configuration_version="deletion-regression-v1",
        state=ProviderOperationState.STARTED,
        reserved_cost_micro_usd=100,
        started_at=FIXTURE_TIME,
        trace_id=uuid4().hex,
    )
    with transactional_session(factory) as session:
        SqlAlchemyProviderOperationRepository(session).add(source.context, operation)
    storage = LocalObjectStorage(tmp_path)
    for item in (source, retained, foreign):
        storage.store_immutable(
            item.context, item.original.storage_key, BytesIO(b"synthetic original")
        )
    uow = create_document_deletion_unit_of_work_factory(factory)
    request = RequestDocumentDeletionService(uow, clock=lambda: PURGE_TIME)
    command = DeleteDocumentsCommand(document_ids=(source.document.id,))
    pending = request.request(source.context, command)
    assert request.request(source.context, command) == pending
    with factory() as session:
        assert (
            SqlAlchemyDocumentRepository(session).get_document_by_id(
                source.context, source.document.id
            )
            is None
        )
        assert session.get(AnswerRow, deleted_answer_id) is not None
    service = PurgeDocumentService(uow, storage, clock=lambda: PURGE_TIME)
    receipt = service.purge(source.context, source.document.id)
    assert receipt.state is DocumentDeletionState.COMPLETED
    assert service.purge(source.context, source.document.id) == receipt
    assert request.request(source.context, command) == (receipt,)
    with factory() as session:
        for model, retained_count in (
            (DocumentRow, 1),
            (DocumentVersionRow, 1),
            (OriginalArtifactRow, 1),
            (ProcessingRunRow, 1),
            (StructureBlockRow, 2),
            (ChunkSetRow, 1),
            (ChunkRow, 2),
            (ResearchRequestRow, 1),
            (RetrievalRunRow, 1),
            (RetrievalCandidateRow, 2),
            (AnswerRow, 1),
            (AnswerStatementRow, 2),
            (EvidenceSpanRow, 1),
            (StatementEvidenceRow, 1),
            (AnswerValidationRow, 1),
        ):
            rows = tuple(
                session.scalars(
                    select(model).where(model.workspace_id == source.context.workspace_id)
                )
            )
            assert len(rows) == retained_count, model.__tablename__
        assert session.get(DocumentRow, source.document.id) is None
        assert session.get(DocumentVersionRow, source.version.id) is None
        assert session.get(OriginalArtifactRow, source.original.id) is None
        assert session.get(ProcessingRunRow, source.processing_run.id) is None
        assert session.get(ResearchRequestRow, source.request.id) is None
        assert session.get(AnswerRow, deleted_answer_id) is None
        assert session.get(AnswerRow, retained_answer_id) is not None
        assert session.get(AnswerRow, foreign_answer_id) is not None
        repository = SqlAlchemyProviderOperationRepository(session)
        stored = repository.get_by_id(source.context, operation.id)
        assert stored is not None and stored.owner_deleted
        assert stored.research_request_id is stored.processing_run_id is None
        summary = repository.summarize_cost(
            source.context, started_at_from=FIXTURE_TIME, started_at_before=PURGE_TIME
        )
        assert summary.active_reserved_cost_micro_usd == 100
    # A delayed provider response must still finalize costs without resurrecting its answer.
    with transactional_session(factory) as session:
        terminal = operation.model_copy(
            update={
                "state": ProviderOperationState.SUCCEEDED,
                "actual_cost_micro_usd": 30,
                "input_units": 1,
                "output_units": 1,
                "duration_ms": 1,
                "completed_at": FIXTURE_TIME + timedelta(milliseconds=1),
            }
        )
        assert SqlAlchemyProviderOperationRepository(session).finalize(source.context, terminal)
    with factory() as session:
        summary = SqlAlchemyProviderOperationRepository(session).summarize_cost(
            source.context, started_at_from=FIXTURE_TIME, started_at_before=PURGE_TIME
        )
        assert summary.actual_cost_micro_usd == 30
        assert summary.active_reserved_cost_micro_usd == 0
    with (
        pytest.raises(ObjectNotFoundError),
        storage.open(source.context, source.original.storage_key),
    ):
        pytest.fail("purged original remains accessible")
    for item in (retained, foreign):
        with storage.open(item.context, item.original.storage_key) as original:
            assert original.read() == b"synthetic original"


@pytest.mark.parametrize("target_kind", ["unknown", "foreign"])
def test_invalid_batch_never_partially_excludes_valid_target(
    integration_database_engine: Engine,
    target_kind: str,
) -> None:
    source = make_research_source(label="Atomic batch")
    other = make_research_source(label="Foreign batch target")
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, other)
    factory = create_session_factory(integration_database_engine)
    request = RequestDocumentDeletionService(create_document_deletion_unit_of_work_factory(factory))
    target = uuid4() if target_kind == "unknown" else other.document.id
    with pytest.raises(DocumentDeletionUnavailableError, match="unavailable"):
        request.request(
            source.context, DeleteDocumentsCommand(document_ids=(source.document.id, target))
        )
    with factory() as session:
        document = session.get(DocumentRow, source.document.id)
        assert document is not None and document.lifecycle_state is DocumentLifecycleState.ACTIVE
        assert (
            session.get(DocumentDeletionRow, (source.context.workspace_id, source.document.id))
            is None
        )


@pytest.mark.parametrize(
    "state", [ProcessingState.PARSING, ProcessingState.LEXICAL_READY, ProcessingState.INDEXING]
)
@pytest.mark.parametrize("expired", [False, True])
def test_started_processing_refuses_entire_batch(
    integration_database_engine: Engine, state: ProcessingState, expired: bool
) -> None:
    source = make_research_source(label="Finished batch source")
    busy = related_source(source)
    busy = replace(
        busy,
        processing_run=busy.processing_run.model_copy(
            update={
                "state": state,
                "attempt_count": 1,
                "started_at": FIXTURE_TIME + timedelta(seconds=1),
                "completed_at": None,
                "lease_id": uuid4(),
                "lease_expires_at": PURGE_TIME + timedelta(minutes=-1 if expired else 1),
                "embedding_config_version": "deletion-test-embedding-v1",
                "dense_index_config_version": "deletion-test-dense-v1",
            }
        ),
    )
    persist_research_source(integration_database_engine, source)
    persist_related_source(integration_database_engine, busy)
    factory = create_session_factory(integration_database_engine)
    with pytest.raises(DocumentDeletionBusyError):
        RequestDocumentDeletionService(
            create_document_deletion_unit_of_work_factory(factory), clock=lambda: PURGE_TIME
        ).request(
            source.context,
            DeleteDocumentsCommand(document_ids=(source.document.id, busy.document.id)),
        )
    with factory() as session:
        for document_id in (source.document.id, busy.document.id):
            document = session.get(DocumentRow, document_id)
            assert (
                document is not None and document.lifecycle_state is DocumentLifecycleState.ACTIVE
            )
            assert (
                session.get(DocumentDeletionRow, (source.context.workspace_id, document_id)) is None
            )


def persist_unclaimed_source(engine: Engine) -> ResearchSource:
    """Persist only capture metadata and an unclaimed run, without invented derived output."""

    source = make_research_source(label="Unclaimed queued deletion")
    source = replace(
        source,
        processing_run=source.processing_run.model_copy(
            update={
                "state": ProcessingState.CAPTURED,
                "attempt_count": 0,
                "started_at": None,
                "completed_at": None,
            }
        ),
    )
    persist_workspace(engine, source)
    factory = create_session_factory(engine)
    with transactional_session(factory) as session:
        SqlAlchemyDocumentRepository(session).add_capture(
            source.context, source.document, source.version, source.original
        )
    with transactional_session(factory) as session:
        SqlAlchemyProcessingRepository(session).add_run(source.context, source.processing_run)
    return source


def test_unclaimed_queued_source_is_purged_and_delayed_claim_is_fenced(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    source = persist_unclaimed_source(integration_database_engine)
    factory = create_session_factory(integration_database_engine)
    uow = create_document_deletion_unit_of_work_factory(factory)
    command = DeleteDocumentsCommand(document_ids=(source.document.id,))
    receipts = RequestDocumentDeletionService(uow, clock=lambda: PURGE_TIME).request(
        source.context, command
    )
    assert receipts[0].state is DocumentDeletionState.PENDING
    claimed_run = source.processing_run.claim_for_processing(
        lease_id=uuid4(),
        claimed_at=PURGE_TIME + timedelta(seconds=1),
        lease_expires_at=PURGE_TIME + timedelta(minutes=6),
        maximum_attempts=4,
    )
    # Processing commits its claim before opening storage or invoking the parser. The existing
    # source guard prevents that commit even if a late broker message still contains the run ID.
    with (
        pytest.raises(IntegrityError, match="source is unavailable"),
        transactional_session(factory) as session,
    ):
        repository = SqlAlchemyProcessingRepository(session)
        assert repository.get_run_by_id_for_update(source.context, claimed_run.id) is not None
        assert repository.update_run(source.context, claimed_run)
        session.flush()
    with factory() as session:
        run = session.get(ProcessingRunRow, source.processing_run.id)
        assert run is not None and run.state is ProcessingState.CAPTURED
    with uow() as work:
        manifest = work.deletions.load_manifest(source.context, source.document.id)
        assert manifest.original_keys == (source.original.storage_key,)
        assert manifest.vector_ids == ()
    storage = LocalObjectStorage(tmp_path)
    storage.store_immutable(
        source.context, source.original.storage_key, BytesIO(b"synthetic original")
    )
    completed = PurgeDocumentService(uow, storage, clock=lambda: PURGE_TIME).purge(
        source.context, source.document.id
    )
    assert completed.state is DocumentDeletionState.COMPLETED
    with factory() as session:
        for model, identifier in (
            (DocumentRow, source.document.id),
            (DocumentVersionRow, source.version.id),
            (OriginalArtifactRow, source.original.id),
            (ProcessingRunRow, source.processing_run.id),
        ):
            assert session.get(model, identifier) is None
    with (
        pytest.raises(ObjectNotFoundError),
        storage.open(source.context, source.original.storage_key),
    ):
        pytest.fail("purged queued original remains accessible")


@pytest.mark.parametrize("deletion_first", [False, True])
def test_queued_claim_and_deletion_admission_serialize(
    integration_database_engine: Engine, deletion_first: bool
) -> None:
    source = persist_unclaimed_source(integration_database_engine)
    factory = create_session_factory(integration_database_engine)
    command = DeleteDocumentsCommand(document_ids=(source.document.id,))
    claimed_run = source.processing_run.claim_for_processing(
        lease_id=uuid4(),
        claimed_at=PURGE_TIME,
        lease_expires_at=PURGE_TIME + timedelta(minutes=6),
        maximum_attempts=4,
    )

    def perform(session: Session, *, deletion: bool) -> None:
        if deletion:
            PostgresDocumentDeletionRepository(session).request(source.context, command, PURGE_TIME)
        else:
            repository = SqlAlchemyProcessingRepository(session)
            assert repository.get_run_by_id_for_update(source.context, claimed_run.id) is not None
            assert repository.update_run(source.context, claimed_run)
            session.flush()

    def compete() -> str:
        try:
            with transactional_session(factory) as session:
                session.execute(text("SET LOCAL lock_timeout = '200ms'"))
                perform(session, deletion=not deletion_first)
        except DBAPIError as error:
            return str(getattr(error.orig, "sqlstate", "unknown"))
        return "unexpected_success"

    with ThreadPoolExecutor(max_workers=1) as pool, transactional_session(factory) as session:
        perform(session, deletion=deletion_first)
        assert pool.submit(compete).result(timeout=5) == "55P03"
    if not deletion_first:
        with pytest.raises(DocumentDeletionBusyError):
            RequestDocumentDeletionService(
                create_document_deletion_unit_of_work_factory(factory)
            ).request(source.context, command)


def test_pending_purge_denies_stale_writes_but_never_allows_immutable_updates(
    integration_database_engine: Engine,
) -> None:
    source = make_research_source(label="Purge write races")
    persist_research_source(integration_database_engine, source)
    retrieval = make_retrieval_result(source)
    persist_retrieval_result(integration_database_engine, source, retrieval)
    answer = make_answer_result(source, retrieval)
    persist_answer_result(integration_database_engine, source, answer)
    factory = create_session_factory(integration_database_engine)
    # Active immutable originals remain protected against both UPDATE and DELETE.
    for statement in (
        update(OriginalArtifactRow)
        .where(OriginalArtifactRow.id == source.original.id)
        .values(byte_size=1),
        delete(OriginalArtifactRow).where(OriginalArtifactRow.id == source.original.id),
    ):
        with pytest.raises(DBAPIError), transactional_session(factory) as session:
            session.execute(statement)
    RequestDocumentDeletionService(create_document_deletion_unit_of_work_factory(factory)).request(
        source.context, DeleteDocumentsCommand(document_ids=(source.document.id,))
    )
    for statement in (
        update(DocumentRow)
        .where(DocumentRow.id == source.document.id)
        .values(lifecycle_state="ACTIVE"),
        update(ProcessingRunRow)
        .where(ProcessingRunRow.id == source.processing_run.id)
        .values(error_summary=None),
        update(AnswerRow)
        .where(AnswerRow.id == answer.answer.id)
        .values(rendered_text="stale output"),
        update(RetrievalCandidateRow)
        .where(RetrievalCandidateRow.id == retrieval.candidates[0].id)
        .values(raw_score=0.5),
    ):
        with pytest.raises(IntegrityError), transactional_session(factory) as session:
            session.execute(statement)
    with pytest.raises(DBAPIError), transactional_session(factory) as session:
        session.execute(
            update(OriginalArtifactRow)
            .where(OriginalArtifactRow.id == source.original.id)
            .values(byte_size=1)
        )


def test_external_failure_keeps_all_canonical_data_pending_for_retry(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    source = make_research_source(label="Interrupted purge")
    persist_research_source(integration_database_engine, source)
    answer_id = persist_answer_graph(integration_database_engine, source)
    factory = create_session_factory(integration_database_engine)
    uow = create_document_deletion_unit_of_work_factory(factory)
    RequestDocumentDeletionService(uow).request(
        source.context, DeleteDocumentsCommand(document_ids=(source.document.id,))
    )

    class FailingStorage:
        def delete_original(self, context: WorkspaceContext, storage_key: str) -> None:
            raise ObjectStorageError("synthetic temporary storage failure")

    with pytest.raises(DocumentPurgePendingError):
        PurgeDocumentService(uow, FailingStorage()).purge(source.context, source.document.id)
    with factory() as session:
        assert session.get(AnswerRow, answer_id) is not None
        document = session.get(DocumentRow, source.document.id)
        receipt = session.get(
            DocumentDeletionRow, (source.context.workspace_id, source.document.id)
        )
        assert (
            document is not None
            and document.lifecycle_state is DocumentLifecycleState.PURGE_PENDING
        )
        assert receipt is not None and receipt.state is DocumentDeletionState.PENDING
    completed = PurgeDocumentService(uow, LocalObjectStorage(tmp_path)).purge(
        source.context, source.document.id
    )
    assert completed.state is DocumentDeletionState.COMPLETED


def test_manifest_covers_noncurrent_versions_and_every_dense_run(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    source = make_research_source(label="All derived versions")
    source = replace(
        source,
        processing_run=source.processing_run.model_copy(
            update={
                "state": ProcessingState.SEARCHABLE,
                "embedding_config_version": "embedding-v1",
                "dense_index_config_version": "dense-v1",
            }
        ),
    )
    persist_research_source(integration_database_engine, source)
    factory = create_session_factory(integration_database_engine)
    second_version = source.version.model_copy(update={"id": uuid4(), "version_number": 2})
    second_original = source.original.model_copy(
        update={
            "id": uuid4(),
            "document_version_id": second_version.id,
            "storage_key": f"workspaces/{source.context.workspace_id}/originals/second.pdf",
        }
    )
    with transactional_session(factory) as session:
        session.add(DocumentVersionRow.from_domain(second_version))
        session.flush()
        session.add(OriginalArtifactRow.from_domain(second_original))
    # Failed configurations can still have partially written external vectors.
    failed_run = source.processing_run.model_copy(
        update={
            "id": uuid4(),
            "state": ProcessingState.FAILED,
            "embedding_config_version": "embedding-v2",
            "error_summary": "synthetic interrupted write",
        }
    )
    failed_blocks = tuple(
        block.model_copy(update={"id": uuid4(), "processing_run_id": failed_run.id})
        for block in source.blocks
    )
    failed_set = source.chunk_set.model_copy(
        update={"id": uuid4(), "processing_run_id": failed_run.id, "is_active": False}
    )
    failed_chunks = tuple(
        chunk.model_copy(
            update={
                "id": uuid4(),
                "processing_run_id": failed_run.id,
                "chunk_set_id": failed_set.id,
                "structure_block_id": block.id,
            }
        )
        for chunk, block in zip(source.chunks, failed_blocks, strict=True)
    )
    with transactional_session(factory) as session:
        SqlAlchemyProcessingRepository(session).add_run(source.context, failed_run)
    with transactional_session(factory) as session:
        SqlAlchemyProcessingRepository(session).add_lexical_output(
            source.context, failed_blocks, failed_set, failed_chunks
        )
    storage = LocalObjectStorage(tmp_path)
    for original in (source.original, second_original):
        storage.store_immutable(
            source.context, original.storage_key, BytesIO(b"synthetic original")
        )
    uow = create_document_deletion_unit_of_work_factory(factory)
    RequestDocumentDeletionService(uow).request(
        source.context, DeleteDocumentsCommand(document_ids=(source.document.id,))
    )
    with uow() as work:
        manifest = work.deletions.load_manifest(source.context, source.document.id)
        assert set(manifest.original_keys) == {
            source.original.storage_key,
            second_original.storage_key,
        }
        assert set(manifest.vector_ids) == {
            dense_vector_id(
                source.context, chunk_id=chunk.id, embedding_config_version="embedding-v1"
            )
            for chunk in source.chunks
        } | {
            dense_vector_id(
                source.context, chunk_id=chunk.id, embedding_config_version="embedding-v2"
            )
            for chunk in failed_chunks
        }
    vectors = RecordingVectorPurger()
    assert (
        PurgeDocumentService(uow, storage, vectors).purge(source.context, source.document.id).state
        is DocumentDeletionState.COMPLETED
    )
    assert vectors.calls == [(source.context.workspace_id, manifest.vector_ids)]
    with factory() as session:
        assert session.get(DocumentVersionRow, second_version.id) is None
        assert session.get(OriginalArtifactRow, second_original.id) is None


@pytest.mark.parametrize("explicit_filter", [True, False])
def test_multi_document_answer_is_removed_whole_even_for_archive_wide_research(
    integration_database_engine: Engine,
    tmp_path: Path,
    explicit_filter: bool,
) -> None:
    engine = integration_database_engine
    source = make_research_source(label="Mixed answer deleted source")
    retained = related_source(source)
    persist_research_source(engine, source)
    persist_related_source(engine, retained)
    unrelated_answer = persist_answer_graph(engine, retained)
    mixed = replace(
        source,
        request=source.request.model_copy(
            update={
                "id": uuid4(),
                "document_ids": (source.document.id, retained.document.id)
                if explicit_filter
                else (),
            }
        ),
    )
    factory = create_session_factory(engine)
    with transactional_session(factory) as session:
        SqlAlchemyResearchRepository(session).add_request(source.context, mixed.request)
    retrieval = make_retrieval_result(mixed)
    retrieval = replace(
        retrieval,
        run=retrieval.run.model_copy(
            update={
                "filters": retrieval.run.filters.model_copy(
                    update={"document_ids": mixed.request.document_ids}
                ),
            }
        ),
        candidates=(
            *retrieval.candidates,
            retrieval.candidates[0].model_copy(
                update={
                    "id": uuid4(),
                    "chunk_id": retained.chunks[1].id,
                    "rank": 3,
                    "fused_rank": 3,
                }
            ),
        ),
    )
    persist_retrieval_result(engine, mixed, retrieval)
    answer = make_answer_result(mixed, retrieval)
    persist_answer_result(engine, mixed, answer)
    # The second statement is supported by the retained document: delete the whole
    # answer, not just the statement linked to the selected deletion target.
    block = retained.blocks[1]
    span = EvidenceSpan(
        id=uuid4(),
        workspace_id=source.context.workspace_id,
        document_version_id=retained.version.id,
        structure_block_id=block.id,
        source_locator_kind=block.source_locator_kind,
        page_number=block.page_number,
        source_start_character=None,
        source_end_character=None,
        start_character=0,
        end_character=len(block.text),
        exact_text=block.text,
        text_sha256=sha256(block.text.encode()).hexdigest(),
        created_at=FIXTURE_TIME,
    )
    with transactional_session(factory) as session:
        repository = SqlAlchemyEvidenceRepository(session)
        repository.add_span(source.context, span)
        session.flush()
        repository.add_statement_evidence(
            source.context,
            answer.statements[1].id,
            (
                StatementEvidence(
                    id=uuid4(),
                    workspace_id=source.context.workspace_id,
                    answer_statement_id=answer.statements[1].id,
                    evidence_span_id=span.id,
                    relationship=EvidenceRelationship.SUPPORTS,
                    display_order=0,
                    created_at=FIXTURE_TIME,
                ),
            ),
        )
    uow = create_document_deletion_unit_of_work_factory(factory)
    RequestDocumentDeletionService(uow).request(
        source.context, DeleteDocumentsCommand(document_ids=(source.document.id,))
    )
    PurgeDocumentService(uow, LocalObjectStorage(tmp_path)).purge(
        source.context, source.document.id
    )
    with factory() as session:
        assert session.get(AnswerRow, answer.answer.id) is None
        assert session.get(ResearchRequestRow, mixed.request.id) is None
        assert session.get(AnswerStatementRow, answer.statements[1].id) is None
        assert session.get(AnswerRow, unrelated_answer) is not None
        assert session.get(DocumentRow, retained.document.id) is not None


def test_source_write_lock_serializes_deletion_admission(
    integration_database_engine: Engine,
) -> None:
    source = make_research_source(label="Concurrent source write")
    persist_research_source(integration_database_engine, source)
    retrieval = make_retrieval_result(source)
    persist_retrieval_result(integration_database_engine, source, retrieval)
    factory = create_session_factory(integration_database_engine)
    command = DeleteDocumentsCommand(document_ids=(source.document.id,))

    def bounded_competing_admission() -> str:
        try:
            with transactional_session(factory) as session:
                session.execute(text("SET LOCAL lock_timeout = '200ms'"))
                PostgresDocumentDeletionRepository(session).request(
                    source.context, command, PURGE_TIME
                )
        except DBAPIError as error:
            return str(getattr(error.orig, "sqlstate", "unknown"))
        return "unexpected_success"

    with ThreadPoolExecutor(max_workers=1) as pool, transactional_session(factory) as session:
        # Its trigger holds a shared document lock until this transaction commits.
        session.execute(
            update(RetrievalCandidateRow)
            .where(RetrievalCandidateRow.id == retrieval.candidates[0].id)
            .values(raw_score=0.6)
        )
        assert pool.submit(bounded_competing_admission).result(timeout=5) == "55P03"
    receipts = RequestDocumentDeletionService(
        create_document_deletion_unit_of_work_factory(factory)
    ).request(source.context, command)
    assert receipts[0].state is DocumentDeletionState.PENDING
