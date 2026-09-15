"""Create short-lived file-backed Compose secrets for isolated verification commands."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

COMPOSE_SECRET_FILES = {
    "POSTGRES_PASSWORD": (
        "POSTGRES_PASSWORD_SECRET_FILE",
        "postgres_password",
    ),
    "REDIS_PASSWORD": (
        "REDIS_PASSWORD_SECRET_FILE",
        "redis_password",
    ),
    "SEAWEEDFS_ADMIN_ACCESS_KEY": (
        "SEAWEEDFS_ADMIN_ACCESS_KEY_SECRET_FILE",
        "seaweedfs_admin_access_key",
    ),
    "SEAWEEDFS_ADMIN_SECRET_ACCESS_KEY": (
        "SEAWEEDFS_ADMIN_SECRET_ACCESS_KEY_SECRET_FILE",
        "seaweedfs_admin_secret_access_key",
    ),
    "INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID": (
        "INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID_SECRET_FILE",
        "seaweedfs_application_access_key",
    ),
    "INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY": (
        "INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY_SECRET_FILE",
        "seaweedfs_application_secret_access_key",
    ),
}


class ComposeSecretError(ValueError):
    """Report invalid synthetic secret input without exposing a value."""


@contextmanager
def disposable_compose_secret_environment(
    values: Mapping[str, str],
) -> Iterator[dict[str, str]]:
    """Yield source-file variables for one complete disposable Compose secret set."""

    missing_names = sorted(set(COMPOSE_SECRET_FILES).difference(values))
    unexpected_names = sorted(set(values).difference(COMPOSE_SECRET_FILES))
    if missing_names:
        raise ComposeSecretError("missing Compose secret values: " + ", ".join(missing_names))
    if unexpected_names:
        raise ComposeSecretError("unexpected Compose secret values: " + ", ".join(unexpected_names))
    if any(not value or "\n" in value or "\r" in value for value in values.values()):
        raise ComposeSecretError("Compose secret values must be non-empty single-line strings")

    with TemporaryDirectory(prefix="investment-research-compose-secrets-") as directory:
        root = Path(directory)
        environment: dict[str, str] = {}
        for value_name, (source_name, filename) in COMPOSE_SECRET_FILES.items():
            path = root / filename
            path.write_text(values[value_name], encoding="utf-8")
            path.chmod(0o600)
            environment[source_name] = str(path)
        yield environment
