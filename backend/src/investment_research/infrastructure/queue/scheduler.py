"""Celery Beat scheduling with one fixed, bounded, content-free broker operation."""

from celery import Celery  # type: ignore[import-untyped]
from celery.beat import PersistentScheduler, ScheduleEntry  # type: ignore[import-untyped]

from investment_research.infrastructure.logging import application_logger
from investment_research.infrastructure.queue.celery import (
    REPAIR_OUTBOX_TASK_NAME,
    create_celery_app,
)
from investment_research.infrastructure.queue.outbox import BoundedCeleryOutboxPublisher
from investment_research.modules.delivery.application import OUTBOX_BROKER_OPERATION_SECONDS
from investment_research.modules.delivery.contracts import (
    OUTBOX_REPAIR_INTERVAL_SECONDS,
    OutboxRepairTickV1,
)
from investment_research.modules.delivery.ports import (
    OutboxPublicationInvalidError,
    OutboxPublicationUnavailableError,
)
from investment_research.settings import QueueSettings

REPAIR_SCHEDULE_NAME = "repair-outbox-v1"
SCHEDULE_FILENAME = "/tmp/investment-research-beat-schedule"
_logger = application_logger("delivery.scheduler")


def create_scheduler_application(settings: QueueSettings) -> Celery:
    """Construct configuration only; no worker import, resources, or publication on creation."""

    application = create_celery_app(settings)
    application.conf.update(
        beat_schedule={
            REPAIR_SCHEDULE_NAME: {
                "task": REPAIR_OUTBOX_TASK_NAME,
                "schedule": OUTBOX_REPAIR_INTERVAL_SECONDS,
                "kwargs": {"payload": OutboxRepairTickV1.create().model_dump(mode="json")},
            }
        },
        beat_schedule_filename=SCHEDULE_FILENAME,
        beat_scheduler="investment_research.infrastructure.queue.scheduler:OutboxRepairScheduler",
        beat_max_loop_interval=OUTBOX_REPAIR_INTERVAL_SECONDS,
        result_expires=None,
        task_publish_retry=False,
        outbox_runtime_location=settings.runtime_location,
    )
    return application


class OutboxRepairScheduler(PersistentScheduler):  # type: ignore[misc]
    """Retain Beat's scheduling/state, never its unbounded producer or raw error reporting."""

    @property
    def producer(self) -> None:
        # Beat.tick evaluates this property before calling apply_entry. Do not let its default
        # cached producer connect/retry outside the bounded, broker-only helper process.
        return None

    def apply_entry(self, entry: ScheduleEntry, producer: object = None) -> None:
        del producer
        try:
            if (
                entry.name != REPAIR_SCHEDULE_NAME
                or entry.task != REPAIR_OUTBOX_TASK_NAME
                or entry.args != ()
                or entry.kwargs != {"payload": OutboxRepairTickV1.create().model_dump(mode="json")}
                or entry.options != {}
            ):
                _logger.error("delivery.repair_tick.entry_rejected")
                return
            settings = QueueSettings.model_validate(
                {
                    "runtime_location": self.app.conf.outbox_runtime_location,
                    "redis_url": self.app.conf.broker_url,
                }
            )
            BoundedCeleryOutboxPublisher(settings).publish_repair_tick(
                timeout_seconds=OUTBOX_BROKER_OPERATION_SECONDS
            )
        except OutboxPublicationUnavailableError:
            # Lost ticks do not own business intent. Wait until the next scheduled interval.
            _logger.warning("delivery.repair_tick.unavailable")
        except OutboxPublicationInvalidError:
            _logger.error("delivery.repair_tick.invalid")
        except Exception:
            # Stop rather than continue after an unexpected failure; never pass raw SDK errors
            # to Celery Beat's default exception/traceback logging.
            raise RuntimeError("repair tick scheduling failed") from None
        else:
            _logger.info("delivery.repair_tick.published")
        finally:
            self._tasks_since_sync += 1
            if self.should_sync():
                self._do_sync()
