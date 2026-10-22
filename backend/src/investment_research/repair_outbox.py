"""Explicit local maintenance command for one canonical, bounded outbox repair pass."""

import argparse
import json
import sys
from collections.abc import Sequence
from contextlib import ExitStack

from sqlalchemy import Engine

from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
)
from investment_research.infrastructure.health import PostgreSQLReadinessProbe
from investment_research.infrastructure.queue.outbox import BoundedCeleryOutboxPublisher
from investment_research.modules.delivery.application import DrainStopReason
from investment_research.modules.delivery.recovery import RecoveryStopReason
from investment_research.modules.delivery.repair import OutboxRepairResult, RepairOutboxService
from investment_research.modules.identity.application import FixedLocalIdentityContextProvider
from investment_research.modules.processing.application import (
    M0_0_PROCESSING_PROFILE,
    M1_PROCESSING_PROFILE,
)
from investment_research.settings import (
    OutboxRepairSettings,
    QueueSettings,
    load_outbox_repair_settings,
)


def create_repair_service(settings: OutboxRepairSettings, engine: Engine) -> RepairOutboxService:
    """Compose only maintenance resources; never initialize worker/provider clients."""

    profile = (
        M0_0_PROCESSING_PROFILE
        if settings.processing_profile == "m0-regression"
        else M1_PROCESSING_PROFILE
    )
    return RepairOutboxService(
        unit_of_work_factory=create_outbox_delivery_unit_of_work_factory(
            create_session_factory(engine), maximum_processing_attempts=profile.maximum_attempts
        ),
        publisher=BoundedCeleryOutboxPublisher(
            QueueSettings(runtime_location=settings.runtime_location, redis_url=settings.redis_url)
        ),
        maximum_attempts=profile.maximum_attempts,
    )


def report_result(result: OutboxRepairResult) -> int:
    """Report only bounded pass observations, not task completion or global queue health."""

    incomplete = (
        result.unstarted.stop_reason is RecoveryStopReason.DEADLINE
        or result.expired.stop_reason is RecoveryStopReason.DEADLINE
        or result.delivery.stop_reason
        in {
            DrainStopReason.DEADLINE,
            DrainStopReason.BROKER_UNAVAILABLE,
            DrainStopReason.INVALID_PUBLICATION,
        }
        or result.delivery.rejected_receipts > 0
        or result.unstarted.blocked > 0
        or result.expired.blocked > 0
    )
    print(
        json.dumps(
            {
                "event": "outbox.repair.pass",
                "status": "attention_required" if incomplete else "pass_completed",
                "used_targets": result.used_targets,
                "unstarted": {
                    "scanned": result.unstarted.scanned,
                    "prepared": result.unstarted.prepared,
                    "blocked": result.unstarted.blocked,
                    "stop_reason": result.unstarted.stop_reason,
                },
                "expired": {
                    "scanned": result.expired.scanned,
                    "prepared": result.expired.prepared,
                    "blocked": result.expired.blocked,
                    "stop_reason": result.expired.stop_reason,
                },
                "delivery": {
                    "scanned": result.delivery.scanned,
                    "claimed": result.delivery.claimed,
                    "published": result.delivery.published,
                    "rejected_receipts": result.delivery.rejected_receipts,
                    "stop_reason": result.delivery.stop_reason,
                },
            }
        ),
        flush=True,
    )
    return 1 if incomplete else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run once in server-owned local scope; no force, scope override, loop, or reset option."""

    parser = argparse.ArgumentParser(description="Run one bounded local outbox repair pass.")
    parser.parse_args(argv)
    stage = "configuration"
    try:
        settings = load_outbox_repair_settings()
        settings.require_database_url()
        settings.require_redis_url()
        stage = "database"
        with ExitStack() as resources:
            engine = create_database_engine(settings)
            resources.callback(engine.dispose)
            if not PostgreSQLReadinessProbe(engine).is_ready():
                print("Outbox repair failed at stage=database; details withheld.", file=sys.stderr)
                return 1
            stage = "repair"
            result = create_repair_service(settings, engine).repair(
                FixedLocalIdentityContextProvider().resolve()
            )
            stage = "cleanup"
        stage = "report"
        return report_result(result)
    except Exception:
        # Neither settings errors nor database/broker exceptions may disclose credentials,
        # document contents, storage targets, or driver details at this operator boundary.
        print(f"Outbox repair failed at stage={stage}; details withheld.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
