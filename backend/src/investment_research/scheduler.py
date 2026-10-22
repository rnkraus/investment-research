"""Explicit, separate Celery Beat process; never imported by the API or worker."""

import argparse
import signal
import sys
from collections.abc import Sequence
from types import FrameType

from celery.beat import Service  # type: ignore[import-untyped]

from investment_research.infrastructure.logging import configure_logging
from investment_research.infrastructure.queue.scheduler import (
    OutboxRepairScheduler,
    create_scheduler_application,
)
from investment_research.settings import load_scheduler_settings


def stop_scheduler(signum: int, frame: FrameType | None) -> None:
    """Interrupt Beat's sleep; its service handles shutdown and synchronizes disposable state."""

    del signum, frame
    raise SystemExit(0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the single local outbox repair scheduler.")
    parser.parse_args(argv)
    stage = "configuration"
    try:
        settings = load_scheduler_settings()
        settings.require_redis_url()
        configure_logging(level=settings.log_level, process_name="scheduler")
        stage = "scheduler"
        with create_scheduler_application(settings) as application:
            service = Service(application, scheduler_cls=OutboxRepairScheduler)
            previous = signal.signal(signal.SIGTERM, stop_scheduler)
            try:
                service.start()
            finally:
                signal.signal(signal.SIGTERM, previous)
        return 0
    except Exception:
        print(f"Outbox scheduler failed at stage={stage}; details withheld.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
