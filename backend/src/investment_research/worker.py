"""Celery worker entry point for canonical document-processing jobs."""

from celery import Celery  # type: ignore[import-untyped]
from celery.signals import worker_process_shutdown  # type: ignore[import-untyped]

from investment_research.bootstrap import WorkerContainer, build_worker_container
from investment_research.infrastructure.queue.celery import create_celery_app
from investment_research.infrastructure.queue.tasks import register_processing_tasks
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.domain import ProcessingRun
from investment_research.settings import WorkerSettings, load_worker_settings


class WorkerProcessingJobExecutor:
    """Own process-local resources and lazily execute validated processing jobs."""

    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._container: WorkerContainer | None = None

    def execute(self, job: ProcessingJobV1) -> ProcessingRun:
        """Build this process's resources on first use and execute one job."""

        return self._get_container().execute_processing_job_service.execute(job)

    def close(self) -> None:
        """Release this process's database and provider resources once."""

        if self._container is None:
            return
        self._container.close()
        self._container = None

    def _get_container(self) -> WorkerContainer:
        """Return the process-owned container, creating it after worker startup."""

        if self._container is None:
            self._container = build_worker_container(self._settings)
        return self._container


def create_worker_application(
    settings: WorkerSettings,
) -> tuple[Celery, WorkerProcessingJobExecutor]:
    """Build a broker application and register its processing task adapter."""

    if settings.processing_profile == "m1":
        settings.require_m1_processing_configuration()
    application = create_celery_app(settings)
    executor = WorkerProcessingJobExecutor(settings)
    register_processing_tasks(application, executor)
    return application, executor


worker_settings = load_worker_settings()
app, processing_job_executor = create_worker_application(worker_settings)


@worker_process_shutdown.connect(weak=False)  # type: ignore[untyped-decorator]
def close_worker_resources(**_: object) -> None:
    """Dispose resources owned by the worker child that is shutting down."""

    processing_job_executor.close()
