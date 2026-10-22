"""One in-process work/time budget shared by sequential delivery phases, not authorization."""

from collections.abc import Callable
from time import monotonic

OUTBOX_SWEEP_MAX_TARGETS = 20
OUTBOX_SWEEP_SECONDS = 30


def validate_target_limit(limit: int) -> None:
    if not 1 <= limit <= OUTBOX_SWEEP_MAX_TARGETS:
        raise ValueError("outbox target limit must be between one and twenty")


class OutboxSweepBudget:
    """Single-invocation, single-thread budget; never persist or share it across workers.

    Preparing and claiming the same target consume separate slots conservatively. Time limits
    prevent further work and bound broker calls, not in-flight SQL or mandatory receipt cleanup.
    """

    def __init__(self, monotonic_clock: Callable[[], float] = monotonic) -> None:
        self._clock = monotonic_clock
        self._deadline = monotonic_clock() + OUTBOX_SWEEP_SECONDS
        self._used_targets = 0

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._clock())

    @property
    def remaining_targets(self) -> int:
        return OUTBOX_SWEEP_MAX_TARGETS - self._used_targets

    @property
    def used_targets(self) -> int:
        return self._used_targets

    def monotonic_now(self) -> float:
        return self._clock()

    def consume_target(self) -> None:
        """Count returned/attempted target work even if SQL crossed the time deadline."""

        if self.remaining_targets <= 0:
            raise ValueError("outbox sweep target budget is exhausted")
        self._used_targets += 1
