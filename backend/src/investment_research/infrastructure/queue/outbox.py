"""Bound a complete synchronous broker operation with a disposable subprocess."""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from math import isfinite
from time import monotonic
from typing import Literal

from pydantic import ValidationError

from investment_research.infrastructure.queue.outbox_transport import (
    MAX_TRANSPORT_INPUT_BYTES,
    TRANSPORT_INVALID,
    TRANSPORT_UNAVAILABLE,
)
from investment_research.modules.delivery.application import OUTBOX_BROKER_OPERATION_SECONDS
from investment_research.modules.delivery.ports import (
    OutboxPublicationInvalidError,
    OutboxPublicationUnavailableError,
)
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.settings_models import QueueSettings


@dataclass(frozen=True, slots=True)
class BoundedCeleryOutboxPublisher:
    """Works in prefork workers without trying to spawn multiprocessing daemon children."""

    settings: QueueSettings

    def is_ready(self, *, timeout_seconds: float) -> bool:
        try:
            self._operate("probe", None, timeout_seconds=timeout_seconds)
        except OutboxPublicationUnavailableError:
            return False
        return True

    def publish(self, job: ProcessingJobV1, *, timeout_seconds: float) -> None:
        try:
            job = ProcessingJobV1.model_validate(job.model_dump())
        except ValidationError:
            raise OutboxPublicationInvalidError(
                "processing publication contract is invalid"
            ) from None
        self._operate("publish", job, timeout_seconds=timeout_seconds)

    def _operate(
        self,
        operation: Literal["probe", "publish", "repair_tick"],
        job: ProcessingJobV1 | None,
        *,
        timeout_seconds: float,
    ) -> None:
        if (
            not isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= OUTBOX_BROKER_OPERATION_SECONDS
        ):
            raise OutboxPublicationInvalidError("broker timeout is outside its accepted bound")
        if self.settings.redis_url is None:
            raise OutboxPublicationInvalidError("broker publication configuration is incomplete")
        deadline = monotonic() + timeout_seconds
        payload = json.dumps(
            {
                "operation": operation,
                "broker_url": self.settings.require_redis_url(),
                "runtime_location": self.settings.runtime_location,
                "deadline": deadline,
                "job": None if job is None else job.model_dump(mode="json"),
            }
        ).encode("utf-8")
        if len(payload) > MAX_TRANSPORT_INPUT_BYTES:
            raise OutboxPublicationInvalidError("broker operation exceeds its transport bound")
        # Do not inherit database, object-storage, provider, or Celery override variables.
        # PYTHONPATH is needed by the existing container's source installation.
        environment = {
            "PATH": os.defpath,
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "investment_research.infrastructure.queue.outbox_transport"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                close_fds=True,
            )
            try:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, timeout_seconds)
                process.communicate(input=payload, timeout=remaining)
            finally:
                if process.poll() is None:
                    process.kill()
                # Reap on timeout/error too; never leave a timed-out publisher running.
                process.communicate()
        except OSError, subprocess.TimeoutExpired:
            raise OutboxPublicationUnavailableError(
                "broker operation failed or timed out"
            ) from None
        if monotonic() >= deadline or process.returncode == TRANSPORT_UNAVAILABLE:
            raise OutboxPublicationUnavailableError("broker operation failed or timed out")
        if process.returncode == TRANSPORT_INVALID:
            raise OutboxPublicationInvalidError("broker publication configuration is invalid")
        if process.returncode != 0:
            raise RuntimeError("outbox broker helper failed unexpectedly")

    def publish_repair_tick(self, *, timeout_seconds: float) -> None:
        """Send only the fixed wake-up signal through the same private bounded transport."""

        self._operate("repair_tick", None, timeout_seconds=timeout_seconds)
