"""Run one explicitly approved, bounded M1 embedding and Pinecone compatibility smoke."""

import argparse
import hashlib
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from investment_research.infrastructure.search.openai import (
    OpenAIEmbeddingProvider,
    create_openai_embedding_provider,
)
from investment_research.infrastructure.search.pinecone import (
    PineconeDenseIndexWriter,
    create_pinecone_dense_index_writer,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.application import (
    build_dense_index_vector,
    dense_index_namespace,
    validate_embedding_response,
)
from investment_research.modules.search.contracts import (
    DenseIndexWriteRequest,
    EmbeddingInput,
    EmbeddingPurpose,
    EmbeddingRequest,
)
from investment_research.settings import WorkerSettings, load_worker_settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APPROVED_MANIFEST = REPOSITORY_ROOT / "fixtures/m1/manifest.yaml"
APPROVED_MANIFEST_SHA256 = "604a4ccb4d7175bf8b6031b4a70617be516a53f69af93f15248af8db7d383955"
APPROVED_TEXT_FIXTURE = (
    REPOSITORY_ROOT / "fixtures/m1/documents/northstar-grid-analytics-fy2026-capacity-update.txt"
)
APPROVED_TEXT_FIXTURE_SHA256 = "45985d7e98d74a7690513624914b49c6d9180842097f837798ce3f42803639b5"
APPROVED_TEXT_FIXTURE_BYTES = 550
APPROVED_PASSAGE = (
    "The primary constraint on new customer deployments is a shortage of high-voltage transformers."
)
APPROVED_PASSAGE_SHA256 = "42e268188a9227904fb39c5e4f6b28edb55e71cbbfab2509d8cde798dd8de978"
APPROVED_PASSAGE_START = 185
APPROVED_PASSAGE_END = 279

MAXIMUM_TOTAL_SPEND_USD = Decimal("0.10")
MAXIMUM_EMBEDDING_INPUT_TOKENS = 5_000
MAXIMUM_EMBEDDING_REQUESTS = 4
MAXIMUM_PINECONE_VECTORS = 64
EMBEDDING_USD_PER_MILLION_TOKENS = Decimal("0.02")
READINESS_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.5


class ProviderSmokeVerificationError(RuntimeError):
    """Report one bounded smoke invariant without provider or fixture details."""


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_approved_passage(manifest_path: Path) -> str:
    """Require the frozen public manifest and return its one approved smoke passage."""

    if manifest_path.resolve() != APPROVED_MANIFEST.resolve():
        raise ProviderSmokeVerificationError("only the approved M1 manifest may be used")
    manifest_bytes = manifest_path.read_bytes()
    if _sha256_bytes(manifest_bytes) != APPROVED_MANIFEST_SHA256:
        raise ProviderSmokeVerificationError("the approved M1 manifest digest does not match")

    fixture_bytes = APPROVED_TEXT_FIXTURE.read_bytes()
    if (
        len(fixture_bytes) != APPROVED_TEXT_FIXTURE_BYTES
        or _sha256_bytes(fixture_bytes) != APPROVED_TEXT_FIXTURE_SHA256
    ):
        raise ProviderSmokeVerificationError("the approved text fixture does not match")
    fixture_text = fixture_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    passage = fixture_text[APPROVED_PASSAGE_START:APPROVED_PASSAGE_END]
    if (
        passage != APPROVED_PASSAGE
        or fixture_text.count(APPROVED_PASSAGE) != 1
        or _sha256_bytes(passage.encode("utf-8")) != APPROVED_PASSAGE_SHA256
    ):
        raise ProviderSmokeVerificationError("the approved smoke passage does not match")
    return passage


def _preflight_budget(passage: str) -> Decimal:
    """Conservatively prove this one request remains below the frozen manifest limits."""

    conservative_input_tokens = len(passage.encode("utf-8"))
    embedding_requests = 1
    pinecone_vectors = 1
    estimated_spend = (
        Decimal(conservative_input_tokens) * EMBEDDING_USD_PER_MILLION_TOKENS / Decimal(1_000_000)
    )
    if conservative_input_tokens > MAXIMUM_EMBEDDING_INPUT_TOKENS:
        raise ProviderSmokeVerificationError("embedding input exceeds the approved token ceiling")
    if embedding_requests > MAXIMUM_EMBEDDING_REQUESTS:
        raise ProviderSmokeVerificationError("embedding request ceiling would be exceeded")
    if pinecone_vectors > MAXIMUM_PINECONE_VECTORS:
        raise ProviderSmokeVerificationError("Pinecone vector ceiling would be exceeded")
    if estimated_spend > MAXIMUM_TOTAL_SPEND_USD:
        raise ProviderSmokeVerificationError("approved provider spend ceiling would be exceeded")
    return estimated_spend


def _await_visible(
    writer: PineconeDenseIndexWriter,
    context: WorkspaceContext,
    vector_id: str,
) -> None:
    """Require the one vector to become visible within the frozen readiness deadline."""

    deadline = monotonic() + READINESS_TIMEOUT_SECONDS
    while monotonic() < deadline:
        if tuple(writer.fetch_visible(context, (vector_id,))) == (vector_id,):
            return
        sleep(POLL_INTERVAL_SECONDS)
    raise ProviderSmokeVerificationError("provider smoke vector did not become visible")


def _delete_and_verify_namespace(
    writer: PineconeDenseIndexWriter,
    context: WorkspaceContext,
    vector_id: str | None,
) -> None:
    """Delete only this run's unique namespace and require the written ID to disappear."""

    writer.index.delete(
        delete_all=True,
        namespace=dense_index_namespace(context),
        timeout=writer.operation_timeout_seconds,
    )
    if vector_id is None:
        return

    deadline = monotonic() + READINESS_TIMEOUT_SECONDS
    while monotonic() < deadline:
        if not writer.fetch_visible(context, (vector_id,)):
            return
        sleep(POLL_INTERVAL_SECONDS)
    raise ProviderSmokeVerificationError("provider smoke cleanup could not be verified")


def verify_m1_provider_smoke(
    *,
    manifest_path: Path,
    settings: WorkerSettings,
) -> Decimal:
    """Execute one bounded compatibility request and always clean its derived namespace."""

    passage = _load_approved_passage(manifest_path)
    estimated_spend = _preflight_budget(passage)
    providers = settings.require_indexing_providers()
    context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())
    input_id = uuid4()
    document_version_id = uuid4()
    chunk_set_id = uuid4()
    embedding_provider: OpenAIEmbeddingProvider | None = None
    writer: PineconeDenseIndexWriter | None = None
    vector_id: str | None = None
    primary_error: Exception | None = None

    try:
        embedding_provider = create_openai_embedding_provider(
            api_key=providers.embedding.api_key.get_secret_value(),
            embedding_config_version=providers.embedding.config_version,
            model=providers.embedding.model,
            dimensions=providers.embedding.dimensions,
            operation_timeout_seconds=providers.embedding.timeout_seconds,
        )
        writer = create_pinecone_dense_index_writer(
            api_key=providers.pinecone.api_key.get_secret_value(),
            index_host=providers.pinecone.index_host,
            operation_timeout_seconds=providers.pinecone.operation_timeout_seconds,
        )
        request = EmbeddingRequest(
            inputs=(
                EmbeddingInput(
                    input_id=input_id,
                    text=passage,
                    purpose=EmbeddingPurpose.DOCUMENT,
                ),
            ),
            embedding_config_version=providers.embedding.config_version,
        )
        embeddings = validate_embedding_response(request, embedding_provider.embed(request))
        vector = build_dense_index_vector(
            context,
            chunk_id=input_id,
            document_version_id=document_version_id,
            chunk_set_id=chunk_set_id,
            embedding=embeddings[0],
            dense_index_config_version=providers.pinecone.config_version,
        )
        vector_id = vector.vector_id
        result = writer.upsert(
            context,
            DenseIndexWriteRequest(
                vectors=(vector,),
                dense_index_config_version=providers.pinecone.config_version,
            ),
        )
        if result.vector_ids != (vector_id,):
            raise ProviderSmokeVerificationError("provider smoke write was not acknowledged")
        _await_visible(writer, context, vector_id)
    except Exception as error:
        primary_error = error
    finally:
        cleanup_error: Exception | None = None
        if writer is not None:
            try:
                _delete_and_verify_namespace(writer, context, vector_id)
            except Exception as error:
                cleanup_error = error
            try:
                writer.close()
            except Exception as error:
                cleanup_error = cleanup_error or error
        if embedding_provider is not None:
            try:
                embedding_provider.close()
            except Exception as error:
                cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            raise ProviderSmokeVerificationError("provider smoke cleanup failed") from None
    if primary_error is not None:
        raise ProviderSmokeVerificationError("provider smoke request failed safely") from None
    return estimated_spend


def build_argument_parser() -> argparse.ArgumentParser:
    """Require explicit public-data, plan, and manifest confirmation."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--confirm-public-synthetic", action="store_true", required=True)
    parser.add_argument("--confirm-pinecone-starter", action="store_true", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Load least-privilege worker credentials and report only safe smoke metrics."""

    arguments = build_argument_parser().parse_args(argv)
    try:
        estimated_spend = verify_m1_provider_smoke(
            manifest_path=arguments.manifest,
            settings=load_worker_settings(),
        )
    except Exception:
        print("M1 provider smoke failed; provider details were withheld.", file=sys.stderr)
        return 1
    print(
        "M1 provider smoke passed: embedding_requests=1, pinecone_vectors=1, "
        f"estimated_embedding_spend_usd={estimated_spend:.8f}, cleanup=verified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
