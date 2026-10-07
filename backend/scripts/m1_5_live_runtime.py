"""Own disposable PostgreSQL and role-specific clients for the opt-in evaluation tool."""

import os
import shlex
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from sqlalchemy import URL, Engine, create_engine

from investment_research.infrastructure.evidence.openai import (
    create_openai_statement_support_evaluator,
    statement_support_prompt_version,
)
from investment_research.infrastructure.generation.openai import create_openai_answer_generator
from investment_research.infrastructure.generation.openai_citable_units import (
    create_openai_citable_unit_answer_generator,
)
from investment_research.infrastructure.search.openai import create_openai_embedding_provider
from investment_research.infrastructure.search.pinecone import (
    create_pinecone_dense_index_writer,
    create_pinecone_dense_retriever,
)
from investment_research.settings import ApiSettings, WorkerSettings
from scripts.docker_secrets import COMPOSE_SECRET_FILES, disposable_compose_secret_environment
from scripts.m1_5_live_index import IsolatedEvaluationIndex, LiveIndexCleanupError
from scripts.m1_5_live_providers import (
    BudgetedAnswerGenerator,
    BudgetedEmbeddingProvider,
    BudgetedSupportEvaluator,
    LiveProviderCalls,
)
from scripts.m1_5_local_output import LocalEvaluationOutput, require_local_terminal
from scripts.plan_m1_5_live_baseline import (
    LiveBaselineLimitError,
    LiveBaselinePlan,
    answer_policy_for_prompt_version,
    support_policy_for_prompt_version,
)
from scripts.verify_postgres import (
    BACKEND_ROOT,
    COMPOSE_FILE,
    COMPOSE_TEST_FILE,
    POSTGRES_DATABASE,
    POSTGRES_USER,
    find_available_loopback_port,
    find_docker_executable,
    generate_disposable_password,
)


class LiveRuntimeError(RuntimeError):
    """A fixed orchestration failure with no environment or subprocess output."""


class _Closable(Protocol):
    def close(self) -> None: ...


def _run(command: Sequence[str], environment: Mapping[str, str], *, cleanup: bool = False) -> None:
    print("$ " + shlex.join(command), flush=True)
    try:
        result = subprocess.run(
            command,
            cwd=BACKEND_ROOT,
            env=environment,
            capture_output=True,
            timeout=60 if cleanup else 120,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        raise LiveRuntimeError("evaluation database command failed") from None
    if result.returncode:
        raise LiveRuntimeError("evaluation database command failed")


@contextmanager
def isolated_database() -> Iterator[Engine]:
    """Never accept a caller-supplied database URL or Compose project name."""

    if find_docker_executable() is None:
        raise LiveRuntimeError("Docker is required for the evaluation database")
    project = "investment-research-m1-5-eval-" + uuid4().hex[:12]
    port = find_available_loopback_port()
    secret_values = {name: generate_disposable_password() for name in COMPOSE_SECRET_FILES}
    url = URL.create(
        "postgresql+psycopg",
        username=POSTGRES_USER,
        password=secret_values["POSTGRES_PASSWORD"],
        host="127.0.0.1",
        port=port,
        database=POSTGRES_DATABASE,
    )
    command = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--file",
        str(COMPOSE_FILE),
        "--file",
        str(COMPOSE_TEST_FILE),
    ]
    # Database subprocesses do not need OpenAI/Pinecone credentials or application secrets.
    base_environment = {
        name: value
        for name in (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "XDG_RUNTIME_DIR",
        )
        if (value := os.environ.get(name)) is not None
    }
    with disposable_compose_secret_environment(secret_values) as secret_sources:
        compose_environment = (
            base_environment
            | secret_sources
            | {
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "POSTGRES_DB": POSTGRES_DATABASE,
                "POSTGRES_USER": POSTGRES_USER,
                "POSTGRES_PORT": str(port),
                "INVESTMENT_RESEARCH_S3_BUCKET": "investment-research-m1-5-evaluation",
            }
        )
        migration_environment = base_environment | {
            "INVESTMENT_RESEARCH_ENVIRONMENT": "test",
            "INVESTMENT_RESEARCH_RUNTIME_LOCATION": "host",
            "INVESTMENT_RESEARCH_DATABASE_URL": url.render_as_string(hide_password=False),
        }
        engine: Engine | None = None
        try:
            _run([*command, "up", "--detach", "--wait", "postgres"], compose_environment)
            _run(
                [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
                migration_environment,
            )
            engine = create_engine(url, hide_parameters=True, pool_pre_ping=True)
            yield engine
        finally:
            try:
                if engine is not None:
                    engine.dispose()
            finally:
                _run(
                    [*command, "down", "--volumes", "--remove-orphans"],
                    compose_environment,
                    cleanup=True,
                )


@dataclass(frozen=True, slots=True, repr=False)
class ProviderResources:
    index: IsolatedEvaluationIndex
    document_embedding: BudgetedEmbeddingProvider
    query_embedding: BudgetedEmbeddingProvider
    generator: BudgetedAnswerGenerator
    support: BudgetedSupportEvaluator


@contextmanager
def provider_resources(
    worker: WorkerSettings,
    api: ApiSettings,
    plan: LiveBaselinePlan,
    calls: LiveProviderCalls,
    cleanup_steps: list[str],
    *,
    local_output: LocalEvaluationOutput | None = None,
) -> Iterator[ProviderResources]:
    """Use restricted role credentials; content inspection is an explicit local-only option."""

    if local_output is not None:
        require_local_terminal()

    document = worker.require_indexing_providers()
    query = api.query_providers
    generation = api.generation_provider
    if (
        query is None
        or generation is None
        or document.pinecone.index_host != query.pinecone.index_host
    ):
        raise LiveRuntimeError("complete matching evaluation provider configuration is required")
    if any(
        config.embedding.config_version != plan.configuration.embedding_config_version
        or config.pinecone.config_version != plan.configuration.dense_index_config_version
        for config in (document, query)
    ):
        raise LiveRuntimeError("evaluation provider versions do not match the approved plan")
    try:
        support_policy = support_policy_for_prompt_version(
            plan.configuration.support_prompt_version
        )
    except LiveBaselineLimitError:
        raise LiveRuntimeError("evaluation support prompt is not approved") from None
    expected_support_prompt = statement_support_prompt_version(
        attribution=support_policy == "attribution", explanations=local_output is not None
    )
    if plan.configuration.support_prompt_version != expected_support_prompt:
        raise LiveRuntimeError("evaluation support prompt does not match the inspection mode")

    def close_client(client: _Closable, label: str) -> None:
        try:
            client.close()
        except Exception:
            cleanup_steps.append(label)

    def close_index(index: IsolatedEvaluationIndex) -> None:
        try:
            index.close()
        except LiveIndexCleanupError as error:
            cleanup_steps.extend(error.steps)
        except Exception:
            cleanup_steps.append("index_cleanup")

    with ExitStack() as clients:
        embeddings = []
        for label, config in (("document_client_close", document), ("query_client_close", query)):
            provider = create_openai_embedding_provider(
                api_key=config.embedding.api_key.get_secret_value(),
                embedding_config_version=config.embedding.config_version,
                operation_timeout_seconds=config.embedding.timeout_seconds,
            )
            clients.callback(close_client, provider, label)
            embeddings.append(BudgetedEmbeddingProvider(provider, calls))
        answer_factory = (
            create_openai_citable_unit_answer_generator
            if answer_policy_for_prompt_version(plan.configuration.answer_prompt_version)
            == "citable_units"
            else create_openai_answer_generator
        )
        answer = answer_factory(
            api_key=generation.api_key.get_secret_value(),
            model=plan.configuration.answer_model,
            local_proposal_output=None if local_output is None else local_output.answer_proposal,
        )
        clients.callback(close_client, answer, "answer_client_close")
        support = create_openai_statement_support_evaluator(
            api_key=generation.api_key.get_secret_value(),
            model=plan.configuration.support_model,
            local_validation_output=None if local_output is None else local_output.support_failure,
            include_decision_reason=local_output is not None,
            include_attribution_context=support_policy == "attribution",
        )
        clients.callback(close_client, support, "support_client_close")
        with ExitStack() as index_clients:
            writer = create_pinecone_dense_index_writer(
                api_key=document.pinecone.api_key.get_secret_value(),
                index_host=document.pinecone.index_host,
                operation_timeout_seconds=document.pinecone.operation_timeout_seconds,
            )
            index_clients.callback(close_client, writer, "writer_close")
            retriever = create_pinecone_dense_retriever(
                api_key=query.pinecone.api_key.get_secret_value(),
                index_host=query.pinecone.index_host,
                operation_timeout_seconds=query.pinecone.operation_timeout_seconds,
            )
            index_clients.callback(close_client, retriever, "query_index_close")
            index = IsolatedEvaluationIndex(
                writer, retriever, maximum_vectors=plan.maximum_document_vectors
            )
            # The index now owns cleanup verification and both client handles.
            index_clients.pop_all()
        clients.callback(close_index, index)
        yield ProviderResources(
            index=index,
            document_embedding=embeddings[0],
            query_embedding=embeddings[1],
            generator=BudgetedAnswerGenerator(answer, calls),
            support=BudgetedSupportEvaluator(support, calls, local_output=local_output),
        )
