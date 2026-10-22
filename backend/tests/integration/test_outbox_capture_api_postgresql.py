"""Normal HTTP capture composition uses canonical intent, not direct Celery dispatch."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from investment_research import bootstrap
from investment_research.api.contracts import DocumentAccepted
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.main import create_app
from investment_research.modules.delivery.dispatch import DispatchCapturedProcessingService
from investment_research.modules.delivery.domain import OutboxState
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.delivery.ports import (
    OutboxPublicationInvalidError,
    OutboxPublicationUnavailableError,
    OutboxPublisher,
)
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import ProcessingRunRow
from investment_research.settings import ApiSettings
from tests.integration.test_document_capture_postgresql import PDF_CONTENT, create_workspace


@pytest.mark.parametrize("profile", ["m0-regression", "m1"])
@pytest.mark.parametrize("media", ["pdf", "text"])
@pytest.mark.parametrize("outcome", ["published", "unavailable", "uncertain", "invalid"])
def test_http_capture_retains_original_and_records_only_its_own_publication(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    profile: str,
    media: str,
    outcome: str,
) -> None:
    context = create_workspace(integration_database_engine)
    identity = Mock()
    identity.resolve.return_value = context
    monkeypatch.setattr(bootstrap, "FixedLocalIdentityContextProvider", lambda: identity)
    publisher = Mock(spec=OutboxPublisher)
    publisher.is_ready.return_value = False
    monkeypatch.setattr(bootstrap, "BoundedCeleryOutboxPublisher", lambda settings: publisher)
    settings = ApiSettings.model_validate(
        {
            **integration_api_settings.model_dump(),
            "object_storage_root": tmp_path / "objects",
            "processing_profile": profile,
        }
    )
    sessions = create_session_factory(integration_database_engine)
    application = create_app(settings)

    with TestClient(application) as client:
        # Leave older due work in the same workspace; an immediate upload must not drain it.
        previous_response = client.post(
            "/api/v1/documents/text",
            json={"title": "Previous synthetic capture", "text": "Earlier capacity report."},
        )
        assert previous_response.status_code == 202
        previous = DocumentAccepted.model_validate(previous_response.json())
        publisher.is_ready.reset_mock()
        publisher.is_ready.return_value = outcome != "unavailable"

        def publish(job: ProcessingJobV1, *, timeout_seconds: float) -> None:
            assert 0 < timeout_seconds <= 2
            assert job.workspace_id == context.workspace_id
            assert job.processing_run_id != previous.processing_run_id
            # A separate connection must be able to lock capture/claim immediately.
            with sessions.begin() as session:
                event = session.scalars(
                    select(OutboxEventRow)
                    .where(OutboxEventRow.target_id == job.processing_run_id)
                    .with_for_update(nowait=True)
                ).one()
                run = session.scalars(
                    select(ProcessingRunRow)
                    .where(ProcessingRunRow.id == job.processing_run_id)
                    .with_for_update(nowait=True)
                ).one()
                assert event.state is OutboxState.CLAIMED and event.attempt_count == 1
                assert run.state is ProcessingState.CAPTURED and run.attempt_count == 0
            if outcome == "uncertain":
                raise OutboxPublicationUnavailableError("synthetic-private-broker-detail")
            if outcome == "invalid":
                raise OutboxPublicationInvalidError("synthetic-private-broker-detail")

        publisher.publish.side_effect = publish
        if media == "pdf":
            content = PDF_CONTENT
            response = client.post(
                "/api/v1/documents",
                data={"title": "Synthetic HTTP PDF capture", "language": "en"},
                files={"file": ("synthetic.pdf", content, "application/pdf")},
            )
        else:
            content = b"Capacity expanded in Europe."
            response = client.post(
                "/api/v1/documents/text",
                json={"title": "Synthetic HTTP text capture", "text": content.decode()},
            )
        assert response.status_code == 202
        accepted = DocumentAccepted.model_validate(response.json())
        assert accepted.state == "CAPTURED"
        original = client.get(
            f"/api/v1/documents/{accepted.document_id}"
            f"/versions/{accepted.document_version_id}/original"
        )
        assert original.status_code == 200 and original.content == content
        status = client.get(accepted.processing_status_url)
        assert status.status_code == 200 and status.json()["state"] == "CAPTURED"
        dispatcher = application.state.container.processing_dispatcher
        assert isinstance(dispatcher, DispatchCapturedProcessingService)
        assert (
            dispatcher.identity_context_provider
            is application.state.container.identity_context_provider
        )
        assert "synthetic-private-broker-detail" not in response.text + caplog.text

    publisher.is_ready.assert_called_once_with(timeout_seconds=2)
    assert publisher.publish.call_count == (0 if outcome == "unavailable" else 1)
    with sessions() as session:
        events = session.scalars(
            select(OutboxEventRow).where(OutboxEventRow.workspace_id == context.workspace_id)
        ).all()
        assert len(events) == 2
        prior = next(event for event in events if event.target_id == previous.processing_run_id)
        target = next(event for event in events if event.target_id == accepted.processing_run_id)
        assert prior.state is OutboxState.PENDING and prior.attempt_count == 0
        assert target.generation == 0
        assert (
            target.state
            is {
                "published": OutboxState.PUBLISHED,
                "unavailable": OutboxState.PENDING,
                "uncertain": OutboxState.RETRY_WAIT,
                "invalid": OutboxState.BLOCKED,
            }[outcome]
        )
        assert target.attempt_count == (0 if outcome == "unavailable" else 1)
