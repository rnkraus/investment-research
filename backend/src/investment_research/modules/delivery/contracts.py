"""Content-free repair trigger; workspace authority stays inside the worker."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class OutboxRepairTickV1(BaseModel):
    """An internal wake-up signal, not a business job or workspace authorization."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    schema_version: Literal["outbox-repair-tick.v1"]

    @classmethod
    def create(cls) -> OutboxRepairTickV1:
        return cls(schema_version="outbox-repair-tick.v1")
