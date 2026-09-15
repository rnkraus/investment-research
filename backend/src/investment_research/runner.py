"""Bounded single-job entry point for local M0.0 document processing."""

import argparse
import ctypes
import resource
import signal
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from uuid import UUID

from investment_research.bootstrap import build_worker_container
from investment_research.infrastructure.parsing.pypdf import PypdfDocumentParser
from investment_research.modules.identity.application import IdentityContextProvider
from investment_research.modules.processing.application import ProcessDocumentService
from investment_research.modules.processing.domain import ProcessingRun, ProcessingState
from investment_research.modules.processing.ports import ProcessingExecutionLimitError
from investment_research.settings import ROOT_ENV_FILE, WorkerSettings, load_worker_settings

EXIT_SUCCESS = 0
EXIT_PROCESSING_FAILED = 1
EXIT_CONFIGURATION_FAILED = 2
MACH_TASK_BASIC_INFO = 20
KERN_SUCCESS = 0


class _MachTimeValue(ctypes.Structure):
    """ctypes representation of Darwin's time_value_t."""

    _fields_ = [
        ("seconds", ctypes.c_int32),
        ("microseconds", ctypes.c_int32),
    ]


class _MachTaskBasicInfo(ctypes.Structure):
    """ctypes representation of Darwin's mach_task_basic_info_data_t."""

    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("resident_size_max", ctypes.c_uint64),
        ("user_time", _MachTimeValue),
        ("system_time", _MachTimeValue),
        ("policy", ctypes.c_int32),
        ("suspend_count", ctypes.c_int32),
    ]


def _raise_processing_timeout(signal_number: int, frame: FrameType | None) -> None:
    """Translate the process alarm into an application-safe execution-limit error."""

    del signal_number, frame
    raise ProcessingExecutionLimitError("processing execution time limit reached")


def current_virtual_memory_bytes() -> int:
    """Measure this process's current virtual address space without extra dependencies."""

    linux_statm = Path("/proc/self/statm")
    if linux_statm.is_file():
        try:
            page_count = int(linux_statm.read_text(encoding="ascii").split()[0])
        except (IndexError, OSError, ValueError) as error:
            raise RuntimeError("could not measure current virtual memory") from error
        return page_count * resource.getpagesize()

    if sys.platform == "darwin":
        return _darwin_virtual_memory_bytes()
    raise RuntimeError("virtual-memory measurement is unsupported on this platform")


def _darwin_virtual_memory_bytes() -> int:
    """Read the current task's virtual size through Darwin's public Mach API."""

    system_library = ctypes.CDLL(None)
    task = ctypes.c_uint32.in_dll(system_library, "mach_task_self_").value
    info = _MachTaskBasicInfo()
    count = ctypes.c_uint32(ctypes.sizeof(info) // ctypes.sizeof(ctypes.c_int32))
    task_info = system_library.task_info
    task_info.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    task_info.restype = ctypes.c_int32
    result = task_info(
        task,
        MACH_TASK_BASIC_INFO,
        ctypes.cast(ctypes.byref(info), ctypes.POINTER(ctypes.c_int32)),
        ctypes.byref(count),
    )
    if result != KERN_SUCCESS or info.virtual_size < 1:
        raise RuntimeError("could not measure current virtual memory")
    return int(info.virtual_size)


def limit_process_memory(additional_bytes: int) -> None:
    """Bound additional virtual memory without undercutting the loaded runtime."""

    current_soft, current_hard = resource.getrlimit(resource.RLIMIT_AS)
    requested_soft = current_virtual_memory_bytes() + additional_bytes
    finite_limits = [requested_soft]
    if current_soft != resource.RLIM_INFINITY:
        finite_limits.append(current_soft)
    if current_hard != resource.RLIM_INFINITY:
        finite_limits.append(current_hard)
    target_soft = min(finite_limits)
    resource.setrlimit(resource.RLIMIT_AS, (target_soft, current_hard))


@contextmanager
def processing_timeout(maximum_seconds: int) -> Iterator[None]:
    """Raise a catchable error when one processing attempt exceeds its wall-clock limit."""

    previous_handler = signal.signal(signal.SIGALRM, _raise_processing_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, maximum_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


@dataclass(frozen=True, slots=True)
class SingleJobProcessingExecutor:
    """Apply process bounds and invoke one processing service with trusted local scope."""

    processing_service: ProcessDocumentService
    identity_context_provider: IdentityContextProvider
    timeout_seconds: int
    memory_limit_bytes: int

    def execute(self, processing_run_id: UUID) -> ProcessingRun:
        """Process exactly one canonical run and return its committed terminal state."""

        limit_process_memory(self.memory_limit_bytes)
        context = self.identity_context_provider.resolve()
        with processing_timeout(self.timeout_seconds):
            return self.processing_service.process(context, processing_run_id)


def execute_single_job(
    settings: WorkerSettings,
    processing_run_id: UUID,
) -> ProcessingRun:
    """Build runner-owned resources, execute one run, and always release them."""

    container = build_worker_container(settings, parser=PypdfDocumentParser())
    try:
        executor = SingleJobProcessingExecutor(
            processing_service=container.process_document_service,
            identity_context_provider=container.identity_context_provider,
            timeout_seconds=settings.processing_timeout_seconds,
            memory_limit_bytes=settings.processing_memory_limit_bytes,
        )
        return executor.execute(processing_run_id)
    finally:
        container.close()


def build_argument_parser() -> argparse.ArgumentParser:
    """Describe the intentionally small single-job command-line interface."""

    parser = argparse.ArgumentParser(
        prog="python -m investment_research.runner",
        description="Process one captured M0.0 document outside the FastAPI process.",
    )
    parser.add_argument("processing_run_id", type=UUID, help="Canonical processing-run UUID")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env_file: Path | None = ROOT_ENV_FILE,
) -> int:
    """Run one bounded processing attempt without exposing internal error details."""

    arguments = build_argument_parser().parse_args(argv)
    try:
        settings = load_worker_settings(env_file=env_file)
        settings.require_database_url()
    except (ValueError, RuntimeError) as error:
        del error
        print("Runner configuration is invalid or incomplete.", file=sys.stderr)
        return EXIT_CONFIGURATION_FAILED

    try:
        result = execute_single_job(settings, arguments.processing_run_id)
    except Exception as error:
        del error
        print("Processing run failed safely.", file=sys.stderr)
        return EXIT_PROCESSING_FAILED

    if result.state is ProcessingState.FAILED:
        print("Processing run failed safely.", file=sys.stderr)
        return EXIT_PROCESSING_FAILED

    print(f"Processing run {result.id} reached {result.state.value}.")
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
