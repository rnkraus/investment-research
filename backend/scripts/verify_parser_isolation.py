"""Verify the final M0 parser boundary in an isolated Docker Compose project."""

import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from uuid import uuid4

from scripts.docker_secrets import (
    COMPOSE_SECRET_FILES,
    disposable_compose_secret_environment,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
FIXTURE_PATH = (
    REPOSITORY_ROOT
    / "fixtures"
    / "m0_0"
    / "documents"
    / "northstar-grid-analytics-fy2025-earnings-brief.pdf"
)
WORKER_IMAGE = "investment-research-backend-worker:local"
PARSER_REQUEST_LIMIT_BYTES = 16 * 1024
PARSER_PAGE_LIMIT = 10
PARSER_TEXT_LIMIT = 100_000
PARSER_MEMORY_LIMIT_BYTES = 640 * 1024 * 1024
PARSER_NANO_CPUS = 1_000_000_000
PARSER_PID_LIMIT = 64

PARSER_RUNTIME_PROBE = """\
import os
import socket
from pathlib import Path

from investment_research.settings import (
    PARSER_FORBIDDEN_ENVIRONMENT_NAMES,
    PARSER_FORBIDDEN_ENVIRONMENT_PREFIXES,
)

assert os.getuid() == 65532
assert os.getgid() == 65532
assert not any(
    name in PARSER_FORBIDDEN_ENVIRONMENT_NAMES
    or name.startswith(PARSER_FORBIDDEN_ENVIRONMENT_PREFIXES)
    for name in os.environ
)

status = Path("/proc/self/status").read_text(encoding="utf-8")
assert "CapEff:\\t0000000000000000" in status
assert "NoNewPrivs:\\t1" in status

try:
    Path("/app/parser-isolation-probe").write_text("forbidden", encoding="utf-8")
except OSError:
    pass
else:
    raise AssertionError("parser root filesystem is writable")

temporary_probe = Path("/tmp/parser-isolation-probe")
temporary_probe.write_text("allowed", encoding="utf-8")
assert temporary_probe.read_text(encoding="utf-8") == "allowed"
temporary_probe.unlink()

assert not Path("/var/run/docker.sock").exists()
assert not Path("/workspace").exists()
secret_root = Path("/run/secrets")
assert not secret_root.exists() or not any(secret_root.iterdir())

for host, port in (("redis", 6379), ("1.1.1.1", 443)):
    try:
        socket.create_connection((host, port), timeout=2).close()
    except OSError:
        continue
    raise AssertionError(f"parser unexpectedly reached {host}:{port}")
"""

WORKER_PROTOCOL_PROBE = f"""\
from http.client import HTTPConnection
from pathlib import Path

from investment_research.infrastructure.parsing.client import HttpDocumentParser
from investment_research.infrastructure.parsing.http_protocol import (
    PARSER_HTTP_PATH,
    PARSER_REQUEST_CONTENT_TYPE,
    PARSER_REQUEST_METADATA_HEADER,
)
from investment_research.infrastructure.parsing.pypdf import PypdfDocumentParser
from investment_research.modules.processing.contracts import (
    PDF_PARSER_CONFIG_VERSION_V1,
    ParserFailureV1,
    ParserLimits,
    ParserRequestV1,
)
from investment_research.modules.processing.ports import ParserLimitExceededError

parser_version = PypdfDocumentParser().parser_version
client = HttpDocumentParser(
    endpoint_url="http://parser:8100",
    parser_version=parser_version,
    parser_config_version=PDF_PARSER_CONFIG_VERSION_V1,
    timeout_seconds=5,
    max_request_bytes={PARSER_REQUEST_LIMIT_BYTES},
)
with Path("/fixture.pdf").open("rb") as content:
    document = client.parse(
        content,
        limits=ParserLimits(
            max_pages={PARSER_PAGE_LIMIT},
            max_extracted_characters={PARSER_TEXT_LIMIT},
        ),
    )
assert document.page_count >= 1
assert document.extracted_character_count > 0

try:
    with Path("/fixture.pdf").open("rb") as content:
        client.parse(
            content,
            limits=ParserLimits(max_pages={PARSER_PAGE_LIMIT}, max_extracted_characters=1),
        )
except ParserLimitExceededError:
    pass
else:
    raise AssertionError("parser accepted output above the requested text limit")

metadata = ParserRequestV1(
    parser_version=parser_version,
    parser_config_version=PDF_PARSER_CONFIG_VERSION_V1,
    limits=ParserLimits(
        max_pages={PARSER_PAGE_LIMIT},
        max_extracted_characters={PARSER_TEXT_LIMIT},
    ),
)
connection = HTTPConnection("parser", 8100, timeout=5)
connection.request(
    "POST",
    PARSER_HTTP_PATH,
    body=b"%PDF-" + b"x" * {PARSER_REQUEST_LIMIT_BYTES},
    headers={{
        "Content-Type": PARSER_REQUEST_CONTENT_TYPE,
        PARSER_REQUEST_METADATA_HEADER: metadata.model_dump_json(),
    }},
)
response = connection.getresponse()
body = response.read()
assert response.status == 413
assert ParserFailureV1.model_validate_json(body).code == "request_too_large"
response.close()
connection.close()
"""


class ParserIsolationVerificationError(RuntimeError):
    """Report one failed parser-boundary invariant without exposing credentials."""


def find_docker_executable() -> str | None:
    """Return the Docker executable path when it is available."""

    return shutil.which("docker")


def generate_disposable_credential() -> str:
    """Return one random credential used only by the isolated Compose project."""

    return secrets.token_hex(24)


def run_command(
    label: str,
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one visible command without rendering its environment."""

    print(f"\n==> {label}", flush=True)
    print(f"$ {shlex.join(command)}", flush=True)
    return subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=check,
        text=True,
    )


def capture_command(command: Sequence[str], *, environment: Mapping[str, str]) -> str:
    """Capture machine-readable Docker state without printing its JSON."""

    result = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def require_mapping(parent: Mapping[str, object], name: str) -> Mapping[str, object]:
    """Return one required mapping from untrusted Docker inspection JSON."""

    value = parent.get(name)
    if not isinstance(value, dict):
        raise ParserIsolationVerificationError(f"Docker inspection is missing {name}")
    return cast(Mapping[str, object], value)


def inspect_service(
    compose_command: Sequence[str],
    service: str,
    *,
    environment: Mapping[str, str],
) -> Mapping[str, object]:
    """Return one complete inspection object for exactly one Compose service."""

    container_id = capture_command(
        [*compose_command, "ps", "--all", "--quiet", service],
        environment=environment,
    )
    if not container_id or "\n" in container_id:
        raise ParserIsolationVerificationError(f"expected exactly one {service} container")
    raw_inspection = capture_command(
        ["docker", "inspect", container_id],
        environment=environment,
    )
    inspection = json.loads(raw_inspection)
    if not isinstance(inspection, list) or len(inspection) != 1:
        raise ParserIsolationVerificationError("Docker returned an invalid inspection result")
    item = inspection[0]
    if not isinstance(item, dict):
        raise ParserIsolationVerificationError("Docker returned an invalid inspection result")
    return cast(Mapping[str, object], item)


def require_no_published_ports(host_config: Mapping[str, object], service: str) -> None:
    """Reject any host binding for one internal-only service."""

    bindings = host_config.get("PortBindings")
    if isinstance(bindings, dict) and any(bindings.values()):
        raise ParserIsolationVerificationError(f"{service} publishes a host port")


def require_confinement(
    inspection: Mapping[str, object],
    *,
    service: str,
    expected_user: str,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Verify controls shared by the internal Redis and parser services."""

    config = require_mapping(inspection, "Config")
    host_config = require_mapping(inspection, "HostConfig")
    if config.get("User") != expected_user:
        raise ParserIsolationVerificationError(f"{service} has an unexpected runtime user")
    if host_config.get("ReadonlyRootfs") is not True:
        raise ParserIsolationVerificationError(f"{service} root filesystem is not read-only")
    if host_config.get("CapDrop") != ["ALL"]:
        raise ParserIsolationVerificationError(f"{service} does not drop all capabilities")
    security_options = host_config.get("SecurityOpt")
    if not isinstance(security_options, list) or not any(
        "no-new-privileges" in str(option) for option in security_options
    ):
        raise ParserIsolationVerificationError(f"{service} lacks no-new-privileges")
    require_no_published_ports(host_config, service)
    return config, host_config


def verify_parser_metadata(
    inspection: Mapping[str, object],
    *,
    project_name: str,
    credentials: Sequence[str],
) -> None:
    """Verify parser secrets, mounts, networks, privileges, and resource ceilings."""

    _, host_config = require_confinement(
        inspection,
        service="parser",
        expected_user="65532:65532",
    )
    if host_config.get("Memory") != PARSER_MEMORY_LIMIT_BYTES:
        raise ParserIsolationVerificationError("parser memory limit does not match the contract")
    if host_config.get("NanoCpus") != PARSER_NANO_CPUS:
        raise ParserIsolationVerificationError("parser CPU limit does not match the contract")
    if host_config.get("PidsLimit") != PARSER_PID_LIMIT:
        raise ParserIsolationVerificationError("parser PID limit does not match the contract")

    mounts = inspection.get("Mounts")
    if not isinstance(mounts, list):
        raise ParserIsolationVerificationError("parser mount metadata is unavailable")
    if any(isinstance(mount, dict) and mount.get("Type") in {"bind", "volume"} for mount in mounts):
        raise ParserIsolationVerificationError("parser has a forbidden bind or volume mount")

    network_settings = require_mapping(inspection, "NetworkSettings")
    networks = require_mapping(network_settings, "Networks")
    if set(networks) != {f"{project_name}_parser"}:
        raise ParserIsolationVerificationError("parser has unexpected network membership")

    serialized = json.dumps(inspection, sort_keys=True)
    if any(credential in serialized for credential in credentials):
        raise ParserIsolationVerificationError("parser metadata contains a credential value")
    if "/var/run/docker.sock" in serialized:
        raise ParserIsolationVerificationError("parser receives the Docker socket")


def verify_redis_metadata(
    inspection: Mapping[str, object],
    *,
    project_name: str,
    credentials: Sequence[str],
) -> None:
    """Verify that the transient broker is internal, bounded, and secret-file configured."""

    require_confinement(
        inspection,
        service="redis",
        expected_user="999:1000",
    )
    network_settings = require_mapping(inspection, "NetworkSettings")
    networks = require_mapping(network_settings, "Networks")
    if set(networks) != {f"{project_name}_backend"}:
        raise ParserIsolationVerificationError("redis has unexpected network membership")
    serialized = json.dumps(inspection, sort_keys=True)
    if any(credential in serialized for credential in credentials):
        raise ParserIsolationVerificationError("redis metadata contains a credential value")


def verify_parser_runtime(
    compose_command: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> None:
    """Exercise parser process, filesystem, secret, and network denial controls."""

    run_command(
        "Verify parser runtime isolation",
        [
            *compose_command,
            "exec",
            "--no-TTY",
            "parser",
            "/app/.venv/bin/python",
            "-c",
            PARSER_RUNTIME_PROBE,
        ],
        environment=environment,
    )


def verify_worker_parser_protocol(
    project_name: str,
    *,
    environment: Mapping[str, str],
) -> None:
    """Use the final worker image to exercise accepted and rejected parser requests."""

    run_command(
        "Verify bounded worker-to-parser protocol",
        [
            "docker",
            "run",
            "--rm",
            "--network",
            f"{project_name}_parser",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=64",
            "--memory=256m",
            "--cpus=1",
            "--mount",
            f"type=bind,source={FIXTURE_PATH},target=/fixture.pdf,readonly",
            "--entrypoint",
            "/app/.venv/bin/python",
            WORKER_IMAGE,
            "-c",
            WORKER_PROTOCOL_PROBE,
        ],
        environment=environment,
    )


def build_environment(secret_sources: Mapping[str, str]) -> dict[str, str]:
    """Build explicit synthetic Compose configuration without loading repository secrets."""

    return os.environ.copy() | {
        **secret_sources,
        "COMPOSE_DISABLE_ENV_FILE": "1",
        "POSTGRES_DB": "investment_research",
        "POSTGRES_USER": "investment_research",
        "POSTGRES_PORT": "55432",
        "INVESTMENT_RESEARCH_S3_BUCKET": "synthetic-parser-isolation",
        "INVESTMENT_RESEARCH_MAX_UPLOAD_BYTES": str(PARSER_REQUEST_LIMIT_BYTES),
        "INVESTMENT_RESEARCH_MAX_PDF_PAGES": str(PARSER_PAGE_LIMIT),
        "INVESTMENT_RESEARCH_MAX_EXTRACTED_CHARACTERS": str(PARSER_TEXT_LIMIT),
    }


def verify_parser_isolation() -> int:
    """Build, exercise, and always remove one isolated parser boundary."""

    if find_docker_executable() is None:
        print("Docker is required for parser-isolation verification.", file=sys.stderr)
        return 127
    if not FIXTURE_PATH.is_file():
        print("The frozen M0.0 PDF fixture is unavailable.", file=sys.stderr)
        return 2

    run_id = uuid4().hex[:8]
    project_name = f"investment-research-parser-check-{run_id}"
    credentials = {name: generate_disposable_credential() for name in COMPOSE_SECRET_FILES}
    compose_command = [
        "docker",
        "compose",
        "--project-name",
        project_name,
        "--file",
        str(COMPOSE_FILE),
    ]
    with disposable_compose_secret_environment(credentials) as secret_sources:
        environment = build_environment(secret_sources)
        exit_code = 0
        try:
            run_command(
                "Build Redis, worker, and parser images",
                [*compose_command, "build", "redis", "worker", "parser"],
                environment=environment,
            )
            run_command(
                "Start internal Redis and parser services",
                [*compose_command, "up", "--detach", "--wait", "redis", "parser"],
                environment=environment,
            )
            parser_inspection = inspect_service(
                compose_command,
                "parser",
                environment=environment,
            )
            redis_inspection = inspect_service(
                compose_command,
                "redis",
                environment=environment,
            )
            credential_values = tuple(credentials.values())
            verify_parser_metadata(
                parser_inspection,
                project_name=project_name,
                credentials=credential_values,
            )
            verify_redis_metadata(
                redis_inspection,
                project_name=project_name,
                credentials=credential_values,
            )
            verify_parser_runtime(compose_command, environment=environment)
            verify_worker_parser_protocol(project_name, environment=environment)
        except (
            json.JSONDecodeError,
            ParserIsolationVerificationError,
            subprocess.CalledProcessError,
        ) as error:
            print(f"Parser-isolation verification failed: {error}", file=sys.stderr)
            exit_code = 1
        finally:
            cleanup = run_command(
                "Remove isolated parser verification project",
                [*compose_command, "down", "--volumes", "--remove-orphans"],
                environment=environment,
                check=False,
            )
            if cleanup.returncode != 0 and exit_code == 0:
                exit_code = cleanup.returncode
    return exit_code


def main() -> int:
    """Run the standalone parser-isolation gate."""

    return verify_parser_isolation()


if __name__ == "__main__":
    raise SystemExit(main())
