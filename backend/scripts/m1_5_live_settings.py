"""Load only evaluation providers, including the existing local Compose secret files."""

import os
import stat
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr, ValidationError

from investment_research.settings import (
    PROJECT_ROOT,
    ROOT_ENV_FILE,
    ApiSettings,
    DenseProviderSettings,
    EmbeddingProviderSettings,
    GenerationProviderSettings,
    PineconeProviderSettings,
    RerankingProviderSettings,
    WorkerSettings,
)

SECRET_ROOT = PROJECT_ROOT / ".secrets"
MAX_SECRET_BYTES = 16_384
DOCUMENT_KEY = "INVESTMENT_RESEARCH_OPENAI_DOCUMENT_API_KEY"
INDEX_KEY = "INVESTMENT_RESEARCH_PINECONE_INDEX_API_KEY"
QUERY_KEY = "INVESTMENT_RESEARCH_OPENAI_QUERY_API_KEY"
QUERY_INDEX_KEY = "INVESTMENT_RESEARCH_PINECONE_QUERY_API_KEY"
GENERATION_KEY = "INVESTMENT_RESEARCH_OPENAI_GENERATION_API_KEY"
RERANKING_KEY = "INVESTMENT_RESEARCH_PINECONE_RERANK_API_KEY"
HOST_NAME = "INVESTMENT_RESEARCH_PINECONE_INDEX_HOST"
BUDGET_NAME = "INVESTMENT_RESEARCH_PROVIDER_MONTHLY_BUDGET_MICRO_USD"


class EvaluationConfigurationError(ValueError):
    """Expose only a code-owned setting name and reason, never a value or path."""

    def __init__(self, setting: str, reason: str) -> None:
        super().__init__(f"setting={setting}, reason={reason}")


def _read_secret_file(name: str, raw_path: str) -> str:
    path = Path(raw_path)
    if not path.is_absolute():
        path = SECRET_ROOT.parent / path
    if path.parent != SECRET_ROOT or SECRET_ROOT.is_symlink():
        raise EvaluationConfigurationError(name, "secret_path_outside_local_directory")
    try:
        # No-follow rejects symlinks; nonblocking allows us to reject FIFOs before reading.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as content:
            if not stat.S_ISREG(os.fstat(content.fileno()).st_mode):
                raise EvaluationConfigurationError(name, "secret_file_not_regular")
            data = content.read(MAX_SECRET_BYTES + 1)
        if len(data) > MAX_SECRET_BYTES:
            raise EvaluationConfigurationError(name, "secret_file_too_large")
        return data.decode("utf-8").rstrip("\r\n")
    except OSError, UnicodeError:
        raise EvaluationConfigurationError(name, "secret_file_unavailable_or_invalid") from None


def _credential(name: str, values: dict[str, str | None]) -> SecretStr:
    direct = os.environ.get(name, values.get(name))
    source_name = f"{name}_SECRET_FILE"
    source = os.environ.get(source_name, values.get(source_name))
    # Mounted Docker secrets are intentionally handled only by the application loader.
    if f"{name}_FILE" in os.environ:
        raise EvaluationConfigurationError(name, "use_host_secret_file_configuration")
    if direct is not None and source is not None:
        raise EvaluationConfigurationError(name, "direct_and_file_values_conflict")
    if source is not None:
        direct = _read_secret_file(name, source)
    if not direct or direct != direct.strip():
        raise EvaluationConfigurationError(name, "credential_missing_or_not_normalized")
    return SecretStr(direct)


def _local_values() -> dict[str, str | None]:
    """Load host evaluation configuration without mutating the process environment."""
    try:
        return dict(dotenv_values(ROOT_ENV_FILE)) if ROOT_ENV_FILE.is_file() else {}
    except OSError, UnicodeError:
        raise EvaluationConfigurationError(
            "local_configuration", "unavailable_or_invalid"
        ) from None


def load_evaluation_reranking_settings(
    *,
    monthly_request_limit: int = 0,
    starter_allowance_confirmed: bool = False,
    public_synthetic_content_approved: bool = False,
) -> RerankingProviderSettings | None:
    """Load one separate inference credential only after explicit host-run admission.

    Default/zero quota returns before accessing local configuration or secret files.
    This loader does not enable a runtime profile, check account usage, or make a call.
    """

    admission = {
        "monthly_request_limit": monthly_request_limit,
        "starter_allowance_confirmed": starter_allowance_confirmed,
        "public_synthetic_content_approved": public_synthetic_content_approved,
    }
    try:
        checked = RerankingProviderSettings.model_validate(
            admission | {"api_key": SecretStr("validation-only")}
        )
    except ValidationError:
        raise EvaluationConfigurationError("reranking_admission", "missing_or_invalid") from None
    if checked.monthly_request_limit == 0:
        return None
    values = _local_values()
    try:
        return RerankingProviderSettings.model_validate(
            admission | {"api_key": _credential(RERANKING_KEY, values)}
        )
    except ValidationError:
        raise EvaluationConfigurationError(RERANKING_KEY, "missing_or_invalid") from None


def load_evaluation_settings() -> tuple[WorkerSettings, ApiSettings]:
    """Never load the ordinary database, broker, S3, parser, or diagnostic configuration."""

    values = _local_values()
    host = os.environ.get(HOST_NAME, values.get(HOST_NAME))
    try:
        validated_host = PineconeProviderSettings.model_validate(
            {"api_key": SecretStr("validation-only"), "index_host": host}
        )
    except ValidationError:
        raise EvaluationConfigurationError(HOST_NAME, "missing_or_invalid") from None

    def dense(embedding_name: str, index_name: str) -> DenseProviderSettings:
        return DenseProviderSettings(
            embedding=EmbeddingProviderSettings(api_key=_credential(embedding_name, values)),
            pinecone=PineconeProviderSettings(
                api_key=_credential(index_name, values), index_host=validated_host.index_host
            ),
        )

    document = dense(DOCUMENT_KEY, INDEX_KEY)
    query = dense(QUERY_KEY, QUERY_INDEX_KEY)
    generation = GenerationProviderSettings(api_key=_credential(GENERATION_KEY, values))
    budget = os.environ.get(BUDGET_NAME, values.get(BUDGET_NAME))
    common = {"provider_monthly_budget_micro_usd": budget}
    try:
        worker = WorkerSettings.model_validate(common | {"indexing_providers": document})
        api = ApiSettings.model_validate(
            common | {"query_providers": query, "generation_provider": generation}
        )
    except ValidationError:
        raise EvaluationConfigurationError(BUDGET_NAME, "missing_or_invalid") from None
    return worker, api
