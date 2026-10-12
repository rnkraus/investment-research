"""Validated runtime settings models and their configuration invariants."""

from ipaddress import ip_address
from pathlib import Path
from re import fullmatch
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from investment_research.modules.processing.contracts import (
    MAX_CONFIGURABLE_EXTRACTED_CHARACTERS,
    MAX_CONFIGURABLE_PDF_PAGES,
)
from investment_research.modules.search.reranking_contracts import (
    BGE_RERANK_CONFIG_VERSION,
    BGE_RERANK_MODEL,
)
from investment_research.rag_settings import RagSettings

type RuntimeEnvironment = Literal["development", "test"]
type RuntimeLocation = Literal["host", "container"]
type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
type ObjectStorageProvider = Literal["local", "s3"]
type ProcessingProfileName = Literal["m1", "m0-regression"]
type ResearchProfileName = Literal["m1", "m1-5-citable-units"]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ROOT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_OBJECT_STORAGE_ROOT = PROJECT_ROOT / "output" / "object-storage"
DEFAULT_S3_REGION_NAME = "us-east-1"
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_CONFIGURABLE_UPLOAD_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES = 250
DEFAULT_MAX_EXTRACTED_CHARACTERS = 2_000_000
DEFAULT_PROCESSING_TIMEOUT_SECONDS = 30
MAX_CONFIGURABLE_PROCESSING_TIMEOUT_SECONDS = 300
DEFAULT_PARSER_CHILD_TIMEOUT_SECONDS = 20
MAX_CONFIGURABLE_PARSER_CHILD_TIMEOUT_SECONDS = 295
DEFAULT_PARSER_MAX_CONCURRENT_CHILDREN = 2
MAX_CONFIGURABLE_PARSER_CONCURRENT_CHILDREN = 16
DEFAULT_PARSER_PORT = 8100
DEFAULT_EMBEDDING_MODEL: Literal["text-embedding-3-small"] = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIMENSIONS: Literal[1536] = 1_536
DEFAULT_EMBEDDING_CONFIG_VERSION: Literal["openai-text-embedding-3-small-1536-v1"] = (
    "openai-text-embedding-3-small-1536-v1"
)
DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 30
MAX_EMBEDDING_TIMEOUT_SECONDS = 30
DEFAULT_DENSE_INDEX_CONFIG_VERSION: Literal["pinecone-serverless-cosine-1536-aws-us-east-1-v1"] = (
    "pinecone-serverless-cosine-1536-aws-us-east-1-v1"
)
DEFAULT_PINECONE_OPERATION_TIMEOUT_SECONDS = 20
MAX_PINECONE_OPERATION_TIMEOUT_SECONDS = 20
M1_MAX_PROVIDER_MONTHLY_BUDGET_MICRO_USD = 5_000_000
DOCKER_SECRETS_ROOT = Path("/run/secrets")
POSTGRES_SETTING_NAMES = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
)
REDIS_SETTING_NAMES = (
    "REDIS_PASSWORD",
    "REDIS_PORT",
)
PARSER_FORBIDDEN_ENVIRONMENT_NAMES = frozenset(
    {
        "INVESTMENT_RESEARCH_DATABASE_URL",
        "INVESTMENT_RESEARCH_REDIS_URL",
        "INVESTMENT_RESEARCH_S3_ENDPOINT_URL",
        "INVESTMENT_RESEARCH_S3_ACCESS_KEY",
        "INVESTMENT_RESEARCH_S3_SECRET_KEY",
        "CELERY_BROKER_URL",
    }
)
PARSER_FORBIDDEN_ENVIRONMENT_PREFIXES = (
    "POSTGRES_",
    "REDIS_",
    "MINIO_",
    "AWS_",
    "INVESTMENT_RESEARCH_S3_",
    "INVESTMENT_RESEARCH_OPENAI_",
    "INVESTMENT_RESEARCH_PINECONE_",
    "OPENAI_",
    "PINECONE_",
    "LANGSMITH_",
)


def _is_loopback_host(value: str) -> bool:
    """Return whether a host name or IP address resolves only to this machine."""

    if value == "localhost":
        return True

    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _is_container_bind_host(value: str) -> bool:
    """Return whether a service may bind to this address inside its container."""

    return _is_loopback_host(value) or value == "0.0.0.0"


def _resolve_private_object_storage_root(value: Path) -> Path:
    """Resolve a local object root and keep it outside the frontend web root."""

    resolved_root = (
        value.expanduser() if value.is_absolute() else PROJECT_ROOT / value.expanduser()
    ).resolve(strict=False)
    if resolved_root.is_relative_to(FRONTEND_ROOT):
        raise ValueError("object_storage_root must be outside the frontend web root")
    return resolved_root


class RuntimeSettings(BaseModel):
    """Values shared by all application-owned runtime processes."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    environment: RuntimeEnvironment = "development"
    runtime_location: RuntimeLocation = "host"
    log_level: LogLevel = "INFO"


class DatabaseSettings(RuntimeSettings):
    """Shared configuration for processes that own PostgreSQL access."""

    database_url: SecretStr | None = None

    @model_validator(mode="after")
    def require_supported_postgresql_url(self) -> DatabaseSettings:
        """Restrict PostgreSQL targets to the active local runtime topology."""

        if self.database_url is None:
            return self

        raw_url = self.database_url.get_secret_value()
        try:
            parsed_url = urlsplit(raw_url)
            port = parsed_url.port
        except ValueError as error:
            raise ValueError("database_url must be a valid PostgreSQL URL") from error

        if parsed_url.scheme != "postgresql+psycopg":
            raise ValueError("database_url must use the postgresql+psycopg scheme")
        if self.runtime_location == "container":
            valid_host = parsed_url.hostname == "postgres"
            target = "the Compose postgres service"
        else:
            valid_host = parsed_url.hostname is not None and _is_loopback_host(parsed_url.hostname)
            target = "localhost or a loopback IP address"
        if not valid_host:
            raise ValueError(f"database_url must target {target}")
        if not parsed_url.username or not parsed_url.password:
            raise ValueError("database_url must include a username and password")
        if port is not None and port < 1:
            raise ValueError("database_url port must be between 1 and 65535")
        if parsed_url.path in {"", "/"}:
            raise ValueError("database_url must include a database name")
        if parsed_url.query:
            raise ValueError("database_url must not include query parameters")
        if parsed_url.fragment:
            raise ValueError("database_url must not include a fragment")
        return self

    def require_database_url(self) -> str:
        """Return the database URL only for components that require PostgreSQL."""

        if self.database_url is None:
            raise RuntimeError("PostgreSQL configuration is required for database access")
        return self.database_url.get_secret_value()


class QueueSettings(DatabaseSettings):
    """Shared configuration for API and worker access to the transient broker."""

    redis_url: SecretStr | None = None

    @model_validator(mode="after")
    def require_supported_redis_url(self) -> QueueSettings:
        """Restrict M0 broker access to the authenticated local runtime topology."""

        if self.redis_url is None:
            return self

        raw_url = self.redis_url.get_secret_value()
        try:
            parsed_url = urlsplit(raw_url)
            port = parsed_url.port
        except ValueError as error:
            raise ValueError("redis_url must be a valid Redis URL") from error

        if parsed_url.scheme != "redis":
            raise ValueError("redis_url must use the redis scheme")
        if self.runtime_location == "container":
            valid_host = parsed_url.hostname == "redis"
            target = "the Compose redis service"
        else:
            valid_host = parsed_url.hostname is not None and _is_loopback_host(parsed_url.hostname)
            target = "localhost or a loopback IP address"
        if not valid_host:
            raise ValueError(f"redis_url must target {target}")
        if parsed_url.username not in {None, ""}:
            raise ValueError("redis_url must use the password-only M0 authentication format")
        if not parsed_url.password:
            raise ValueError("redis_url must include a password")
        if port is not None and port < 1:
            raise ValueError("redis_url port must be between 1 and 65535")
        if parsed_url.path != "/0":
            raise ValueError("redis_url must select the M0 broker database /0")
        if parsed_url.query:
            raise ValueError("redis_url must not include query parameters")
        if parsed_url.fragment:
            raise ValueError("redis_url must not include a fragment")
        return self

    def require_redis_url(self) -> str:
        """Return the broker URL only for components that require queue access."""

        if self.redis_url is None:
            raise RuntimeError("Redis configuration is required for queue access")
        return self.redis_url.get_secret_value()


class S3Settings(BaseModel):
    """Validated S3 data-plane configuration shared by API and worker processes."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    endpoint_url: str
    region_name: str = DEFAULT_S3_REGION_NAME
    bucket: str
    access_key_id: SecretStr
    secret_access_key: SecretStr

    @field_validator("region_name")
    @classmethod
    def require_normalized_region_name(cls, value: str) -> str:
        """Accept one non-empty normalized region identifier."""

        if not fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
            raise ValueError("region_name must be a normalized lowercase identifier")
        return value

    @field_validator("bucket")
    @classmethod
    def require_supported_bucket_name(cls, value: str) -> str:
        """Restrict buckets to portable DNS-compatible S3 names."""

        valid = (
            3 <= len(value) <= 63
            and fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", value) is not None
            and ".." not in value
            and ".-" not in value
            and "-." not in value
        )
        if not valid:
            raise ValueError("bucket must be a DNS-compatible S3 bucket name")
        try:
            ip_address(value)
        except ValueError:
            return value
        raise ValueError("bucket must not be formatted as an IP address")

    @field_validator("access_key_id", "secret_access_key")
    @classmethod
    def require_non_empty_secret(cls, value: SecretStr) -> SecretStr:
        """Reject blank credentials without exposing their values."""

        secret = value.get_secret_value()
        if not secret or secret != secret.strip():
            raise ValueError("S3 credentials must be non-empty normalized values")
        return value


class ObjectStorageSettings(QueueSettings):
    """Configuration shared by processes that access canonical originals."""

    object_storage_provider: ObjectStorageProvider = "local"
    object_storage_root: Path = DEFAULT_OBJECT_STORAGE_ROOT
    s3: S3Settings | None = None
    provider_monthly_budget_micro_usd: int | None = Field(
        default=None,
        ge=1,
        le=M1_MAX_PROVIDER_MONTHLY_BUDGET_MICRO_USD,
    )

    @field_validator("object_storage_root")
    @classmethod
    def require_private_object_storage_root(cls, value: Path) -> Path:
        """Resolve test-only local storage and keep it outside the web root."""

        return _resolve_private_object_storage_root(value)

    @model_validator(mode="after")
    def require_matching_object_storage_configuration(self) -> ObjectStorageSettings:
        """Require one complete provider configuration without unused S3 secrets."""

        if self.object_storage_provider == "local":
            if self.s3 is not None:
                raise ValueError("s3 configuration is not allowed for the local provider")
            return self

        if self.s3 is None:
            raise ValueError("s3 configuration is required for the s3 provider")

        try:
            endpoint = urlsplit(self.s3.endpoint_url)
            port = endpoint.port
        except ValueError as error:
            raise ValueError("s3 endpoint_url must be a valid HTTP URL") from error
        if endpoint.scheme not in {"http", "https"}:
            raise ValueError("s3 endpoint_url must use HTTP or HTTPS")
        if endpoint.username or endpoint.password:
            raise ValueError("s3 endpoint_url must not contain credentials")
        if endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment:
            raise ValueError("s3 endpoint_url must not contain a path, query, or fragment")
        if port is not None and port < 1:
            raise ValueError("s3 endpoint_url port must be between 1 and 65535")

        if self.runtime_location == "container":
            valid_host = endpoint.hostname == "seaweedfs-s3"
            target = "the Compose seaweedfs-s3 service"
        else:
            valid_host = endpoint.hostname is not None and _is_loopback_host(endpoint.hostname)
            target = "localhost or a loopback IP address"
        if not valid_host:
            raise ValueError(f"s3 endpoint_url must target {target}")
        return self

    def require_s3_settings(self) -> S3Settings:
        """Return complete S3 values only when the S3 provider is selected."""

        if self.object_storage_provider != "s3" or self.s3 is None:
            raise RuntimeError("S3 configuration is required for S3 object storage")
        return self.s3


class EmbeddingProviderSettings(BaseModel):
    """Fixed M1 embedding configuration plus one role-specific secret."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    api_key: SecretStr
    model: Literal["text-embedding-3-small"] = DEFAULT_EMBEDDING_MODEL
    dimensions: Literal[1536] = DEFAULT_EMBEDDING_DIMENSIONS
    config_version: Literal["openai-text-embedding-3-small-1536-v1"] = (
        DEFAULT_EMBEDDING_CONFIG_VERSION
    )
    timeout_seconds: int = Field(
        default=DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
        ge=1,
        le=MAX_EMBEDDING_TIMEOUT_SECONDS,
    )

    @field_validator("api_key")
    @classmethod
    def require_normalized_api_key(cls, value: SecretStr) -> SecretStr:
        """Reject blank or padded credentials without exposing their values."""

        secret = value.get_secret_value()
        if not secret or secret != secret.strip():
            raise ValueError("embedding API key must be a non-empty normalized value")
        return value


class PineconeProviderSettings(BaseModel):
    """Fixed M1 dense-index configuration plus one role-specific data-plane key."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    api_key: SecretStr
    index_host: str
    config_version: Literal["pinecone-serverless-cosine-1536-aws-us-east-1-v1"] = (
        DEFAULT_DENSE_INDEX_CONFIG_VERSION
    )
    operation_timeout_seconds: int = Field(
        default=DEFAULT_PINECONE_OPERATION_TIMEOUT_SECONDS,
        ge=1,
        le=MAX_PINECONE_OPERATION_TIMEOUT_SECONDS,
    )

    @field_validator("api_key")
    @classmethod
    def require_normalized_api_key(cls, value: SecretStr) -> SecretStr:
        """Reject blank or padded credentials without exposing their values."""

        secret = value.get_secret_value()
        if not secret or secret != secret.strip():
            raise ValueError("Pinecone API key must be a non-empty normalized value")
        return value

    @field_validator("index_host")
    @classmethod
    def require_pinecone_data_plane_host(cls, value: str) -> str:
        """Accept one normalized Pinecone host without scheme, path, port, or credentials."""

        valid = (
            value == value.strip().lower()
            and len(value) <= 253
            and ".." not in value
            and fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]\.pinecone\.io", value) is not None
        )
        if not valid:
            raise ValueError("index_host must be a normalized Pinecone data-plane host")
        return value


class DenseProviderSettings(BaseModel):
    """One complete role-specific embedding and dense-index provider pair."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    embedding: EmbeddingProviderSettings
    pinecone: PineconeProviderSettings


class GenerationProviderSettings(BaseModel):
    """One API-only credential for M1 answer and statement-support calls."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    api_key: SecretStr

    @field_validator("api_key")
    @classmethod
    def require_normalized_api_key(cls, value: SecretStr) -> SecretStr:
        """Reject blank or padded credentials without exposing their values."""

        secret = value.get_secret_value()
        if not secret or secret != secret.strip():
            raise ValueError("generation API key must be a non-empty normalized value")
        return value


class RerankingProviderSettings(BaseModel):
    """Explicit Starter-only experiment admission; never an account-quota measurement."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    api_key: SecretStr
    model: Literal["bge-reranker-v2-m3"] = BGE_RERANK_MODEL
    config_version: Literal["m1-5-pinecone-bge-none-v1"] = BGE_RERANK_CONFIG_VERSION
    billing_policy: Literal["pinecone-starter-bge-included-v1"] = "pinecone-starter-bge-included-v1"
    timeout_seconds: int = Field(default=20, ge=1, le=20, strict=True)
    monthly_request_limit: int = Field(default=0, ge=0, le=500, strict=True)
    starter_allowance_confirmed: bool = Field(default=False, strict=True)
    public_synthetic_content_approved: bool = Field(default=False, strict=True)

    @field_validator("api_key")
    @classmethod
    def require_normalized_api_key(cls, value: SecretStr) -> SecretStr:
        """Reject blank or padded inference credentials without rendering their value."""

        secret = value.get_secret_value()
        if not secret or secret != secret.strip():
            raise ValueError("reranking API key must be a non-empty normalized value")
        return value

    @model_validator(mode="after")
    def require_explicit_admission(self) -> RerankingProviderSettings:
        """A positive local quota requires both owner attestations, not an inferred allowance."""

        if self.monthly_request_limit > 0 and not (
            self.starter_allowance_confirmed and self.public_synthetic_content_approved
        ):
            raise ValueError("reranking requires Starter allowance and public/synthetic approval")
        return self


class ApiSettings(ObjectStorageSettings):
    """Configuration available to the FastAPI process."""

    processing_profile: ProcessingProfileName = "m1"
    research_profile: ResearchProfileName = "m1"
    rag: RagSettings = Field(default_factory=RagSettings)
    app_name: str = "Investment Research Archive"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1024, le=65535)
    max_upload_bytes: int = Field(
        default=DEFAULT_MAX_UPLOAD_BYTES,
        ge=1024,
        le=MAX_CONFIGURABLE_UPLOAD_BYTES,
    )
    query_providers: DenseProviderSettings | None = None
    generation_provider: GenerationProviderSettings | None = None
    acceptance_dense_index_path: Path | None = None
    local_model_failure_output: bool = False

    @model_validator(mode="after")
    def restrict_local_model_failure_output(self) -> ApiSettings:
        """Permit raw model diagnostics only in the local development API container."""

        if self.local_model_failure_output and (
            self.environment != "development" or self.runtime_location != "container"
        ):
            raise ValueError("local model failure output requires a development API container")
        return self

    @model_validator(mode="after")
    def restrict_regression_processing_profile(self) -> ApiSettings:
        """Allow legacy capture only in the isolated M0 test runtime."""

        if self.processing_profile == "m0-regression":
            if self.environment != "test":
                raise ValueError(
                    "m0-regression processing_profile is allowed only in the test environment"
                )
            if (
                self.query_providers is not None
                or self.generation_provider is not None
                or self.acceptance_dense_index_path is not None
            ):
                raise ValueError("m0-regression processing_profile cannot use M1 providers")
        return self

    @model_validator(mode="after")
    def require_supported_api_host(self) -> ApiSettings:
        """Permit a wildcard bind only inside the container runtime."""

        if self.runtime_location == "container":
            valid = _is_container_bind_host(self.api_host)
        else:
            valid = _is_loopback_host(self.api_host)
        if not valid:
            raise ValueError("api_host is not allowed for the selected runtime location")
        return self

    @model_validator(mode="after")
    def require_complete_m1_provider_configuration(self) -> ApiSettings:
        """Enable the complete M1 API provider and budget group or none of it."""

        acceptance_enabled = self.acceptance_dense_index_path is not None
        if acceptance_enabled:
            self._require_safe_acceptance_dense_index_path()
            if self.query_providers is not None or self.generation_provider is not None:
                raise ValueError(
                    "M1 acceptance dense index cannot be combined with external API providers"
                )
            if self.provider_monthly_budget_micro_usd is None:
                raise ValueError("M1 acceptance API configuration requires the monthly budget")
            return self

        if (self.query_providers is None) != (self.generation_provider is None):
            raise ValueError(
                "M1 API provider configuration requires query and generation providers together"
            )
        providers_enabled = self.query_providers is not None
        budget_configured = self.provider_monthly_budget_micro_usd is not None
        if providers_enabled != budget_configured:
            raise ValueError("M1 API provider configuration requires the monthly provider budget")
        return self

    @model_validator(mode="after")
    def require_citable_units_provider_configuration(self) -> ApiSettings:
        """Keep the source-unit profile separate from credential-free regression adapters."""

        if self.research_profile == "m1-5-citable-units":
            if self.processing_profile != "m1":
                raise ValueError("citable-units research_profile requires M1 processing")
            if self.query_providers is None or self.generation_provider is None:
                raise ValueError("citable-units research_profile requires external API providers")
        return self

    def require_acceptance_dense_index_path(self) -> Path:
        """Return the validated shared index path for the isolated test runtime."""

        if self.acceptance_dense_index_path is None:
            raise RuntimeError("M1 acceptance dense index path is required")
        return self.acceptance_dense_index_path

    def _require_safe_acceptance_dense_index_path(self) -> None:
        """Keep the credential-free adapter inside explicit non-secret test configuration."""

        path = self.acceptance_dense_index_path
        assert path is not None
        if self.environment != "test":
            raise ValueError("M1 acceptance dense index is allowed only in the test environment")
        if not path.is_absolute():
            raise ValueError("M1 acceptance dense index path must be absolute")
        if path.is_relative_to(DOCKER_SECRETS_ROOT):
            raise ValueError("M1 acceptance dense index path must not use the secrets directory")

    def require_query_providers(self) -> DenseProviderSettings:
        """Return complete query-provider values only when M1 dense retrieval is enabled."""

        if self.query_providers is None:
            raise RuntimeError("query provider configuration is required")
        return self.query_providers

    def require_generation_provider(self) -> GenerationProviderSettings:
        """Return the API-only generation credential when the M1 path is enabled."""

        if self.generation_provider is None:
            raise RuntimeError("generation provider configuration is required")
        return self.generation_provider


class WorkerSettings(ObjectStorageSettings):
    """Least-privilege configuration available to the Celery worker."""

    processing_profile: ProcessingProfileName = "m1"
    parser_endpoint_url: str | None = None
    max_parser_request_bytes: int = Field(
        default=DEFAULT_MAX_UPLOAD_BYTES,
        ge=1024,
        le=MAX_CONFIGURABLE_UPLOAD_BYTES,
    )
    max_pdf_pages: int = Field(
        default=DEFAULT_MAX_PDF_PAGES,
        ge=1,
        le=MAX_CONFIGURABLE_PDF_PAGES,
    )
    max_extracted_characters: int = Field(
        default=DEFAULT_MAX_EXTRACTED_CHARACTERS,
        ge=1,
        le=MAX_CONFIGURABLE_EXTRACTED_CHARACTERS,
    )
    processing_timeout_seconds: int = Field(
        default=DEFAULT_PROCESSING_TIMEOUT_SECONDS,
        ge=1,
        le=MAX_CONFIGURABLE_PROCESSING_TIMEOUT_SECONDS,
    )
    indexing_providers: DenseProviderSettings | None = None
    acceptance_dense_index_path: Path | None = None

    @model_validator(mode="after")
    def restrict_regression_processing_profile(self) -> WorkerSettings:
        """Keep the credential-free legacy profile inside isolated test runtimes."""

        if self.processing_profile == "m0-regression" and self.environment != "test":
            raise ValueError(
                "m0-regression processing_profile is allowed only in the test environment"
            )
        return self

    @model_validator(mode="after")
    def require_complete_m1_provider_budget(self) -> WorkerSettings:
        """Enable document-index providers and their shared budget together."""

        acceptance_enabled = self.acceptance_dense_index_path is not None
        if acceptance_enabled:
            self._require_safe_acceptance_dense_index_path()
            if self.processing_profile != "m1":
                raise ValueError("M1 acceptance dense index requires the M1 processing profile")
            if self.indexing_providers is not None:
                raise ValueError(
                    "M1 acceptance dense index cannot be combined with external indexing providers"
                )
            if self.provider_monthly_budget_micro_usd is None:
                raise ValueError("M1 acceptance worker configuration requires the monthly budget")
            return self

        providers_enabled = self.indexing_providers is not None
        budget_configured = self.provider_monthly_budget_micro_usd is not None
        if providers_enabled != budget_configured:
            raise ValueError(
                "M1 worker provider configuration requires the monthly provider budget"
            )
        return self

    def require_m1_processing_configuration(self) -> int:
        """Reject an M1 worker that cannot index before it consumes a job."""

        if self.processing_profile != "m1":
            raise ValueError("M1 processing configuration requires the M1 profile")
        if self.acceptance_dense_index_path is None and self.indexing_providers is None:
            raise ValueError("M1 worker requires indexing providers or the test acceptance index")
        if self.provider_monthly_budget_micro_usd is None:
            raise ValueError("M1 worker requires the monthly provider budget")
        return self.provider_monthly_budget_micro_usd

    def require_acceptance_dense_index_path(self) -> Path:
        """Return the validated shared index path for the isolated test runtime."""

        if self.acceptance_dense_index_path is None:
            raise RuntimeError("M1 acceptance dense index path is required")
        return self.acceptance_dense_index_path

    def _require_safe_acceptance_dense_index_path(self) -> None:
        """Keep the credential-free adapter inside explicit non-secret test configuration."""

        path = self.acceptance_dense_index_path
        assert path is not None
        if self.environment != "test":
            raise ValueError("M1 acceptance dense index is allowed only in the test environment")
        if not path.is_absolute():
            raise ValueError("M1 acceptance dense index path must be absolute")
        if path.is_relative_to(DOCKER_SECRETS_ROOT):
            raise ValueError("M1 acceptance dense index path must not use the secrets directory")

    @model_validator(mode="after")
    def require_supported_parser_endpoint(self) -> WorkerSettings:
        """Restrict parser traffic to the isolated local runtime endpoint."""

        if self.parser_endpoint_url is None:
            return self
        try:
            endpoint = urlsplit(self.parser_endpoint_url)
            port = endpoint.port
        except ValueError as error:
            raise ValueError("parser_endpoint_url must be a valid HTTP URL") from error
        if endpoint.scheme != "http":
            raise ValueError("parser_endpoint_url must use HTTP")
        if endpoint.username or endpoint.password:
            raise ValueError("parser_endpoint_url must not contain credentials")
        if endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment:
            raise ValueError("parser_endpoint_url must not contain a path, query, or fragment")
        if port is not None and port < 1:
            raise ValueError("parser_endpoint_url port must be between 1 and 65535")

        if self.runtime_location == "container":
            valid_host = endpoint.hostname == "parser"
            target = "the Compose parser service"
        else:
            valid_host = endpoint.hostname is not None and _is_loopback_host(endpoint.hostname)
            target = "localhost or a loopback IP address"
        if not valid_host:
            raise ValueError(f"parser_endpoint_url must target {target}")
        return self

    def require_parser_endpoint_url(self) -> str:
        """Return the internal endpoint only for workers using isolated parsing."""

        if self.parser_endpoint_url is None:
            raise RuntimeError("parser endpoint configuration is required")
        return self.parser_endpoint_url

    def require_indexing_providers(self) -> DenseProviderSettings:
        """Return complete document-index provider values only when M1 indexing is enabled."""

        if self.indexing_providers is None:
            raise RuntimeError("indexing provider configuration is required")
        return self.indexing_providers


class ParserSettings(RuntimeSettings):
    """Non-secret configuration available to the isolated parser process."""

    parser_host: str = "127.0.0.1"
    parser_port: int = Field(default=DEFAULT_PARSER_PORT, ge=1024, le=65535)
    max_request_bytes: int = Field(
        default=DEFAULT_MAX_UPLOAD_BYTES,
        ge=1024,
        le=MAX_CONFIGURABLE_UPLOAD_BYTES,
    )
    max_pdf_pages: int = Field(
        default=DEFAULT_MAX_PDF_PAGES,
        ge=1,
        le=MAX_CONFIGURABLE_PDF_PAGES,
    )
    max_extracted_characters: int = Field(
        default=DEFAULT_MAX_EXTRACTED_CHARACTERS,
        ge=1,
        le=MAX_CONFIGURABLE_EXTRACTED_CHARACTERS,
    )
    child_timeout_seconds: int = Field(
        default=DEFAULT_PARSER_CHILD_TIMEOUT_SECONDS,
        ge=1,
        le=MAX_CONFIGURABLE_PARSER_CHILD_TIMEOUT_SECONDS,
    )
    max_concurrent_children: int = Field(
        default=DEFAULT_PARSER_MAX_CONCURRENT_CHILDREN,
        ge=1,
        le=MAX_CONFIGURABLE_PARSER_CONCURRENT_CHILDREN,
    )

    @model_validator(mode="after")
    def require_supported_parser_host(self) -> ParserSettings:
        """Permit the internal listener only on an appropriate local bind address."""

        if self.runtime_location == "container":
            valid = _is_container_bind_host(self.parser_host)
        else:
            valid = _is_loopback_host(self.parser_host)
        if not valid:
            raise ValueError("parser_host is not allowed for the selected runtime location")
        return self
