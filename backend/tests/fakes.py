"""Small reusable test doubles for application-owned ports."""

from investment_research.modules.processing.contracts import ProcessingJobV1


class RecordingProcessingDispatcher:
    """Record processing jobs without contacting Celery or Redis."""

    def __init__(self) -> None:
        self.jobs: list[ProcessingJobV1] = []

    def dispatch(self, job: ProcessingJobV1) -> None:
        """Record one validated job for later assertions."""

        self.jobs.append(job)
