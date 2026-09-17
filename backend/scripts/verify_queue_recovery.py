"""Verify real Redis delivery and recovery after a Celery pool-worker loss."""

import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import URL, Engine, create_engine, func, select, update
from sqlalchemy.orm import Session

from investment_research.modules.identity.application import LOCAL_DEVELOPMENT_WORKSPACE_ID
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import (
    ChunkRow,
    ChunkSetRow,
    ProcessingRunRow,
    StructureBlockRow,
)
from scripts.docker_secrets import (
    COMPOSE_SECRET_FILES,
    disposable_compose_secret_environment,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
COMPOSE_TEST_FILE = REPOSITORY_ROOT / "compose.test.yaml"
FIXTURE_PDF = (
    REPOSITORY_ROOT / "fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf"
)
POSTGRES_DATABASE = "investment_research"
POSTGRES_USER = "investment_research"
RECOVERY_LEASE_SECONDS = 10
STATE_TIMEOUT_SECONDS = 45.0
POLL_INTERVAL_SECONDS = 0.25

CAPTURE_PROBE_CODE = """\
from datetime import UTC, datetime
from pathlib import Path

from investment_research.bootstrap import build_api_container
from investment_research.modules.documents.contracts import PdfCaptureInput
from investment_research.modules.processing.domain import ProcessingState
from investment_research.settings import load_api_settings

container = build_api_container(load_api_settings(env_file=None))
try:
    context = container.identity_context_provider.resolve()
    capture_input = PdfCaptureInput(
        filename="northstar-grid-analytics-fy2025-earnings-brief.pdf",
        media_type="application/pdf",
        title="Queue recovery verification fixture",
        language="en",
        published_at=datetime(2026, 2, 20, 12, tzinfo=UTC),
        available_at=datetime(2026, 2, 20, 12, tzinfo=UTC),
    )
    with Path("/verification-fixture.pdf").open("rb") as content:
        result = container.capture_document_service.capture_pdf(context, capture_input, content)
    assert result.state is ProcessingState.CAPTURED
finally:
    container.close()
"""

KILL_POOL_WORKER_CODE = """\
import os
import signal

from investment_research.worker import app

try:
    responses = app.control.inspect(timeout=5).stats()
finally:
    app.close()
if not responses or len(responses) != 1:
    raise SystemExit("expected exactly one Celery worker response")
worker_stats = next(iter(responses.values()))
pool_stats = worker_stats.get("pool")
pool_workers = None if not isinstance(pool_stats, dict) else pool_stats.get("processes")
if not isinstance(pool_workers, list) or len(pool_workers) != 1:
    raise SystemExit("expected exactly one Celery pool worker")
os.kill(int(pool_workers[0]), signal.SIGKILL)
"""


class QueueRecoveryVerificationError(RuntimeError):
    """Report a failed recovery invariant without exposing credentials."""


def find_docker_executable() -> str | None:
    """Return the Docker executable path when it is available."""

    return shutil.which("docker")


def generate_disposable_credential() -> str:
    """Return a random credential used only by one isolated Compose project."""

    return secrets.token_hex(24)


def find_available_loopback_port() -> int:
    """Reserve and release one currently available local TCP port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def run_command(
    label: str,
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    working_directory: Path = REPOSITORY_ROOT,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run one visible command without rendering its environment values."""

    print(f"\n==> {label}", flush=True)
    print(f"$ {shlex.join(command)}", flush=True)
    return subprocess.run(
        command,
        cwd=working_directory,
        env=environment,
        check=check,
    )


def compose_container_id(
    compose_command: Sequence[str],
    service: str,
    *,
    environment: Mapping[str, str],
) -> str:
    """Resolve exactly one running container for a Compose service."""

    result = subprocess.run(
        [*compose_command, "ps", "--quiet", service],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    container_id = result.stdout.strip()
    if not container_id or "\n" in container_id:
        raise QueueRecoveryVerificationError(f"expected one running {service} container")
    return container_id


def create_verification_engine(*, password: str, port: int) -> Engine:
    """Create a host-side engine for observing the disposable canonical state."""

    return create_engine(
        URL.create(
            "postgresql+psycopg",
            username=POSTGRES_USER,
            password=password,
            host="127.0.0.1",
            port=port,
            database=POSTGRES_DATABASE,
        ),
        pool_pre_ping=True,
    )


def find_processing_run_id(engine: Engine) -> UUID:
    """Return the single captured run created by the recovery probe."""

    with Session(engine) as session:
        identifiers = tuple(
            session.scalars(
                select(ProcessingRunRow.id).where(
                    ProcessingRunRow.workspace_id == LOCAL_DEVELOPMENT_WORKSPACE_ID
                )
            )
        )
    if len(identifiers) != 1:
        raise QueueRecoveryVerificationError("expected exactly one captured processing run")
    return identifiers[0]


def wait_for_processing_state(
    engine: Engine,
    processing_run_id: UUID,
    expected_state: ProcessingState,
) -> None:
    """Wait until PostgreSQL exposes one expected processing state."""

    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    last_state: ProcessingState | None = None
    last_attempt_count: int | None = None
    last_lease_expires_at: datetime | None = None
    while time.monotonic() < deadline:
        with Session(engine) as session:
            snapshot = session.execute(
                select(
                    ProcessingRunRow.state,
                    ProcessingRunRow.attempt_count,
                    ProcessingRunRow.lease_expires_at,
                ).where(
                    ProcessingRunRow.id == processing_run_id,
                    ProcessingRunRow.workspace_id == LOCAL_DEVELOPMENT_WORKSPACE_ID,
                )
            ).one_or_none()
        if snapshot is not None:
            last_state, last_attempt_count, last_lease_expires_at = snapshot
        if last_state is expected_state:
            return
        if last_state is ProcessingState.FAILED:
            raise QueueRecoveryVerificationError(
                "processing entered FAILED before the expected recovery state"
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    lease_is_expired = last_lease_expires_at is not None and last_lease_expires_at <= datetime.now(
        UTC
    )
    raise QueueRecoveryVerificationError(
        "processing did not reach "
        f"{expected_state.value} before timeout; "
        f"last_state={getattr(last_state, 'value', None)}, "
        f"attempt_count={last_attempt_count}, "
        f"lease_expired={lease_is_expired}"
    )


def shorten_active_lease(engine: Engine, processing_run_id: UUID) -> None:
    """Compress only the disposable test lease so recovery remains a short gate."""

    retry_at = datetime.now(UTC) + timedelta(seconds=RECOVERY_LEASE_SECONDS)
    with Session(engine) as session, session.begin():
        updated_id = session.scalar(
            update(ProcessingRunRow)
            .where(
                ProcessingRunRow.id == processing_run_id,
                ProcessingRunRow.workspace_id == LOCAL_DEVELOPMENT_WORKSPACE_ID,
                ProcessingRunRow.state == ProcessingState.PARSING,
            )
            .values(lease_expires_at=retry_at)
            .returning(ProcessingRunRow.id)
        )
        if updated_id != processing_run_id:
            raise QueueRecoveryVerificationError("active processing lease could not be shortened")


def verify_single_recovered_output(engine: Engine, processing_run_id: UUID) -> None:
    """Require one reclaimed attempt and one canonical fixture output."""

    with Session(engine) as session:
        run = session.get(ProcessingRunRow, processing_run_id)
        block_count = session.scalar(
            select(func.count())
            .select_from(StructureBlockRow)
            .where(StructureBlockRow.processing_run_id == processing_run_id)
        )
        chunk_set_count = session.scalar(
            select(func.count())
            .select_from(ChunkSetRow)
            .where(ChunkSetRow.processing_run_id == processing_run_id)
        )
        chunk_count = session.scalar(
            select(func.count())
            .select_from(ChunkRow)
            .where(ChunkRow.processing_run_id == processing_run_id)
        )
    if run is None or run.state is not ProcessingState.LEXICAL_READY:
        raise QueueRecoveryVerificationError("recovered processing run is not lexical-ready")
    if run.attempt_count != 2:
        raise QueueRecoveryVerificationError("worker loss did not produce exactly one reclaim")
    if (block_count, chunk_set_count, chunk_count) != (4, 1, 4):
        raise QueueRecoveryVerificationError(
            "recovery did not produce one canonical fixture output"
        )


def verify_queue_recovery() -> int:
    """Run and always remove one isolated worker-loss recovery environment."""

    if find_docker_executable() is None:
        print("Docker is required for queue recovery verification.", file=sys.stderr)
        return 127
    if not FIXTURE_PDF.is_file():
        print("The frozen M0.0 PDF fixture is unavailable.", file=sys.stderr)
        return 2

    run_id = uuid4().hex[:8]
    project_name = f"investment-research-queue-check-{run_id}"
    postgres_port = find_available_loopback_port()
    secret_values = {name: generate_disposable_credential() for name in COMPOSE_SECRET_FILES}
    postgres_password = secret_values["POSTGRES_PASSWORD"]
    compose_command = [
        "docker",
        "compose",
        "--project-name",
        project_name,
        "--file",
        str(COMPOSE_FILE),
        "--file",
        str(COMPOSE_TEST_FILE),
    ]
    python_command = [sys.executable, "-m"]
    parser_container_id: str | None = None
    parser_is_paused = False
    engine: Engine | None = None
    exit_code = 0

    with disposable_compose_secret_environment(secret_values) as secret_sources:
        compose_environment = os.environ.copy() | secret_sources
        compose_environment.update(
            {
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "POSTGRES_DB": POSTGRES_DATABASE,
                "POSTGRES_USER": POSTGRES_USER,
                "POSTGRES_PORT": str(postgres_port),
                "INVESTMENT_RESEARCH_S3_BUCKET": f"queue-recovery-{run_id}",
                "INVESTMENT_RESEARCH_S3_REGION_NAME": "us-east-1",
            }
        )
        migration_environment = os.environ.copy()
        migration_environment.update(
            {
                "INVESTMENT_RESEARCH_ENVIRONMENT": "test",
                "INVESTMENT_RESEARCH_DATABASE_URL": (
                    f"postgresql+psycopg://{POSTGRES_USER}:{postgres_password}"
                    f"@127.0.0.1:{postgres_port}/{POSTGRES_DATABASE}"
                ),
                "INVESTMENT_RESEARCH_REDIS_URL": "redis://:unused@127.0.0.1:1/0",
            }
        )

        try:
            run_command(
                "Build and start isolated processing runtime",
                [*compose_command, "up", "--build", "--detach", "--wait", "worker"],
                environment=compose_environment,
            )
            run_command(
                "Migrate isolated PostgreSQL database",
                [*python_command, "alembic", "-c", "alembic.ini", "upgrade", "head"],
                environment=migration_environment,
                working_directory=BACKEND_ROOT,
            )
            run_command(
                "Seed isolated PostgreSQL workspaces",
                [*python_command, "scripts.seed_m0_0"],
                environment=migration_environment,
                working_directory=BACKEND_ROOT,
            )
            engine = create_verification_engine(password=postgres_password, port=postgres_port)
            parser_container_id = compose_container_id(
                compose_command,
                "parser",
                environment=compose_environment,
            )
            run_command(
                "Pause parser before publishing the recovery probe",
                ["docker", "pause", parser_container_id],
                environment=compose_environment,
            )
            parser_is_paused = True
            run_command(
                "Capture fixture and publish one real Celery job",
                [
                    *compose_command,
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "/app/.venv/bin/python",
                    "--volume",
                    f"{FIXTURE_PDF}:/verification-fixture.pdf:ro",
                    "--env",
                    "INVESTMENT_RESEARCH_API_HOST=0.0.0.0",
                    "worker",
                    "-c",
                    CAPTURE_PROBE_CODE,
                ],
                environment=compose_environment,
            )
            processing_run_id = find_processing_run_id(engine)
            wait_for_processing_state(engine, processing_run_id, ProcessingState.PARSING)
            shorten_active_lease(engine, processing_run_id)

            worker_container_id = compose_container_id(
                compose_command,
                "worker",
                environment=compose_environment,
            )
            worker_loss = run_command(
                "Kill the active Celery pool worker after its canonical claim",
                [
                    "docker",
                    "exec",
                    worker_container_id,
                    "/app/.venv/bin/python",
                    "-c",
                    KILL_POOL_WORKER_CODE,
                ],
                environment=compose_environment,
                check=False,
            )
            if worker_loss.returncode not in {0, 137}:
                raise QueueRecoveryVerificationError(
                    "Celery pool worker could not be terminated for recovery verification"
                )
            run_command(
                "Resume parser for the reclaimed delivery",
                ["docker", "unpause", parser_container_id],
                environment=compose_environment,
            )
            parser_is_paused = False

            wait_for_processing_state(engine, processing_run_id, ProcessingState.LEXICAL_READY)
            verify_single_recovered_output(engine, processing_run_id)
            print("Queue recovery produced one canonical lexical output.", flush=True)
        except (QueueRecoveryVerificationError, subprocess.CalledProcessError) as error:
            if isinstance(error, QueueRecoveryVerificationError):
                print(f"Queue recovery verification failed: {error}", file=sys.stderr)
                exit_code = 1
            else:
                exit_code = error.returncode or 1
            run_command(
                "Show isolated worker logs after verification failure",
                [*compose_command, "logs", "--no-color", "--tail", "200", "worker"],
                environment=compose_environment,
                check=False,
            )
        finally:
            if parser_is_paused and parser_container_id is not None:
                unpause = run_command(
                    "Resume parser before cleanup",
                    ["docker", "unpause", parser_container_id],
                    environment=compose_environment,
                    check=False,
                )
                if unpause.returncode != 0 and exit_code == 0:
                    exit_code = unpause.returncode
            if engine is not None:
                engine.dispose()
            cleanup = run_command(
                "Remove isolated queue-recovery project and its volumes",
                [*compose_command, "down", "--volumes", "--remove-orphans"],
                environment=compose_environment,
                check=False,
            )
            if cleanup.returncode != 0 and exit_code == 0:
                exit_code = cleanup.returncode

    return exit_code


def main() -> int:
    """Run the queue recovery verification command."""

    return verify_queue_recovery()


if __name__ == "__main__":
    raise SystemExit(main())
