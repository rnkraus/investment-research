"""Private broker-only subprocess entry point; credentials travel through stdin, never argv."""

import sys
from time import monotonic
from typing import Literal, Self

from celery.exceptions import CeleryError  # type: ignore[import-untyped]
from kombu.exceptions import KombuError  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from redis.exceptions import AuthenticationError, AuthorizationError, RedisError

from investment_research.infrastructure.queue.celery import (
    PROCESS_DOCUMENT_TASK_NAME,
    PROCESSING_QUEUE_NAME,
    create_celery_app,
)
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.settings_models import QueueSettings

MAX_TRANSPORT_INPUT_BYTES = 8_192
TRANSPORT_UNAVAILABLE = 1
TRANSPORT_INVALID = 2
TRANSPORT_UNEXPECTED = 3


class BrokerOperation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    operation: Literal["probe", "publish"]
    broker_url: SecretStr
    runtime_location: Literal["host", "container"]
    deadline: float = Field(gt=0, allow_inf_nan=False)
    job: ProcessingJobV1 | None = None

    @model_validator(mode="after")
    def require_publish_job(self) -> Self:
        if (self.operation == "publish") != (self.job is not None):
            raise ValueError("transport operation and job must agree")
        return self


def execute_operation(request: BrokerOperation) -> int:
    """One disposable connection and no retries; the parent enforces the wall deadline."""

    settings = QueueSettings(
        runtime_location=request.runtime_location, redis_url=request.broker_url
    )
    remaining = request.deadline - monotonic()
    if remaining <= 0:
        return TRANSPORT_UNAVAILABLE
    with create_celery_app(settings) as application:
        application.conf.update(task_publish_retry=False, broker_connection_retry=False)
        with application.connection_for_write(
            connect_timeout=remaining,
            transport_options={
                "socket_connect_timeout": remaining,
                "socket_timeout": remaining,
                "retry_on_timeout": False,
                "max_retries": 0,
            },
        ) as connection:
            connection.ensure_connection(max_retries=0, timeout=remaining)
            if monotonic() >= request.deadline:
                return TRANSPORT_UNAVAILABLE
            if request.job is not None:
                application.send_task(
                    PROCESS_DOCUMENT_TASK_NAME,
                    kwargs={"payload": request.job.model_dump(mode="json")},
                    task_id=request.job.idempotency_key,
                    queue=PROCESSING_QUEUE_NAME,
                    serializer="json",
                    ignore_result=True,
                    add_to_parent=False,
                    connection=connection,
                    retry=False,
                    retry_policy={"max_retries": 0},
                )
        return 0 if monotonic() < request.deadline else TRANSPORT_UNAVAILABLE


def main() -> int:
    """Read only the private bounded pipe; do not load files or print provider details."""

    try:
        raw = sys.stdin.buffer.read(MAX_TRANSPORT_INPUT_BYTES + 1)
        if len(raw) > MAX_TRANSPORT_INPUT_BYTES:
            return TRANSPORT_INVALID
        return execute_operation(BrokerOperation.model_validate_json(raw))
    except ValidationError, ValueError:
        return TRANSPORT_INVALID
    except (CeleryError, KombuError, RedisError, OSError) as error:
        # Kombu can wrap a Redis authentication/configuration failure. Inspect types,
        # never exception messages, and do not retry it as a transient network outage.
        cause: BaseException | None = error
        for _ in range(4):
            if isinstance(cause, (AuthenticationError, AuthorizationError)):
                return TRANSPORT_INVALID
            cause = None if cause is None else cause.__cause__
        return TRANSPORT_UNAVAILABLE
    except Exception:
        # No exception text/traceback crosses the private process boundary. Unexpected
        # programming faults remain distinct from a retryable broker outage.
        return TRANSPORT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
