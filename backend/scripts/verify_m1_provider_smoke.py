"""Run one explicitly approved, bounded M1 external-provider compatibility smoke."""

import argparse
import hashlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from investment_research.infrastructure.evidence.openai import (
    OPENAI_STATEMENT_SUPPORT_MODEL,
    OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS,
    OpenAIStatementSupportEvaluator,
    create_openai_statement_support_evaluator,
)
from investment_research.infrastructure.generation.openai import (
    OPENAI_ANSWER_MODEL,
    OPENAI_ANSWER_TIMEOUT_SECONDS,
    OpenAIAnswerGenerator,
    create_openai_answer_generator,
)
from investment_research.infrastructure.search.openai import (
    OpenAIEmbeddingProvider,
    create_openai_embedding_provider,
)
from investment_research.infrastructure.search.pinecone import (
    PineconeDenseIndexWriter,
    create_pinecone_dense_index_writer,
)
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationSource,
    UnvalidatedGeneratedAnswer,
)
from investment_research.modules.research.domain import CoverageStatus
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
from investment_research.settings import (
    GenerationProviderSettings,
    WorkerSettings,
    load_generation_provider_settings,
    load_worker_settings,
)

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
APPROVED_ANSWERABLE_QUESTION = "What is mainly holding back Northstar's rollout to new clients?"
APPROVED_UNANSWERABLE_QUESTION = "What dividend did Northstar Grid Analytics declare for FY2026?"

MAXIMUM_TOTAL_SPEND_USD = Decimal("0.10")
MAXIMUM_EMBEDDING_INPUT_TOKENS = 5_000
MAXIMUM_GENERATION_EVALUATION_INPUT_TOKENS = 12_000
MAXIMUM_GENERATION_EVALUATION_OUTPUT_TOKENS = 3_000
MAXIMUM_EMBEDDING_REQUESTS = 4
MAXIMUM_GENERATION_REQUESTS = 2
MAXIMUM_SUPPORT_EVALUATION_REQUESTS = 2
MAXIMUM_PINECONE_VECTORS = 64
EMBEDDING_USD_PER_MILLION_TOKENS = Decimal("0.02")
TERRA_INPUT_USD_PER_MILLION_TOKENS = Decimal("2.00")
TERRA_OUTPUT_USD_PER_MILLION_TOKENS = Decimal("12.00")
SMOKE_ANSWER_MAX_OUTPUT_TOKENS = 1_000
SMOKE_SUPPORT_MAX_OUTPUT_TOKENS = 500
GENERATION_INPUT_ENVELOPE_RESERVE = 2_000
SUPPORT_INPUT_ENVELOPE_RESERVE = 1_000
READINESS_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.5


class ProviderSmokeVerificationError(RuntimeError):
    """Report one bounded smoke invariant without provider or fixture details."""


@dataclass(slots=True)
class ProviderSmokeBudget:
    """Reserve every provider request and its conservative token ceiling before use."""

    embedding_requests: int = 0
    embedding_input_tokens: int = 0
    generation_requests: int = 0
    support_evaluation_requests: int = 0
    generation_evaluation_input_tokens: int = 0
    generation_evaluation_output_tokens: int = 0
    pinecone_vectors: int = 0

    @property
    def estimated_maximum_spend_usd(self) -> Decimal:
        """Return the worst-case OpenAI spend reserved by this invocation."""

        embedding_spend = (
            Decimal(self.embedding_input_tokens)
            * EMBEDDING_USD_PER_MILLION_TOKENS
            / Decimal(1_000_000)
        )
        generation_input_spend = (
            Decimal(self.generation_evaluation_input_tokens)
            * TERRA_INPUT_USD_PER_MILLION_TOKENS
            / Decimal(1_000_000)
        )
        generation_output_spend = (
            Decimal(self.generation_evaluation_output_tokens)
            * TERRA_OUTPUT_USD_PER_MILLION_TOKENS
            / Decimal(1_000_000)
        )
        return embedding_spend + generation_input_spend + generation_output_spend

    def reserve_embedding(self, text: str) -> None:
        """Reserve one embedding request and one Pinecone vector."""

        self.embedding_requests += 1
        self.embedding_input_tokens += len(text.encode("utf-8"))
        self.pinecone_vectors += 1
        self._require_limits()

    def reserve_generation(self, generation_input: AnswerGenerationInput) -> None:
        """Reserve one answer call before sending its bounded payload."""

        self.generation_requests += 1
        self.generation_evaluation_input_tokens += (
            GENERATION_INPUT_ENVELOPE_RESERVE
            + len(generation_input.question.encode("utf-8"))
            + sum(len(source.text.encode("utf-8")) for source in generation_input.sources)
        )
        self.generation_evaluation_output_tokens += SMOKE_ANSWER_MAX_OUTPUT_TOKENS
        self._require_limits()

    def reserve_support(self, statement: str, evidence_texts: Sequence[str]) -> None:
        """Reserve one statement-support call before sending its bounded payload."""

        self.support_evaluation_requests += 1
        self.generation_evaluation_input_tokens += (
            SUPPORT_INPUT_ENVELOPE_RESERVE
            + len(statement.encode("utf-8"))
            + sum(len(text.encode("utf-8")) for text in evidence_texts)
        )
        self.generation_evaluation_output_tokens += SMOKE_SUPPORT_MAX_OUTPUT_TOKENS
        self._require_limits()

    def _require_limits(self) -> None:
        """Fail before a reservation would exceed any frozen manifest ceiling."""

        limits = (
            (
                self.embedding_input_tokens <= MAXIMUM_EMBEDDING_INPUT_TOKENS,
                "embedding input exceeds the approved token ceiling",
            ),
            (
                self.embedding_requests <= MAXIMUM_EMBEDDING_REQUESTS,
                "embedding request ceiling would be exceeded",
            ),
            (
                self.generation_requests <= MAXIMUM_GENERATION_REQUESTS,
                "generation request ceiling would be exceeded",
            ),
            (
                self.support_evaluation_requests <= MAXIMUM_SUPPORT_EVALUATION_REQUESTS,
                "support-evaluation request ceiling would be exceeded",
            ),
            (
                self.generation_evaluation_input_tokens
                <= MAXIMUM_GENERATION_EVALUATION_INPUT_TOKENS,
                "generation and evaluation input exceeds the approved token ceiling",
            ),
            (
                self.generation_evaluation_output_tokens
                <= MAXIMUM_GENERATION_EVALUATION_OUTPUT_TOKENS,
                "generation and evaluation output exceeds the approved token ceiling",
            ),
            (
                self.pinecone_vectors <= MAXIMUM_PINECONE_VECTORS,
                "Pinecone vector ceiling would be exceeded",
            ),
            (
                self.estimated_maximum_spend_usd <= MAXIMUM_TOTAL_SPEND_USD,
                "approved provider spend ceiling would be exceeded",
            ),
        )
        for permitted, message in limits:
            if not permitted:
                raise ProviderSmokeVerificationError(message)


@dataclass(frozen=True, slots=True)
class ProviderSmokeResult:
    """Safe aggregate metrics from one completed provider compatibility smoke."""

    embedding_requests: int
    generation_requests: int
    support_evaluation_requests: int
    pinecone_vectors: int
    estimated_maximum_spend_usd: Decimal


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


def _generation_input(
    *,
    question: str,
    source: AnswerGenerationSource,
) -> AnswerGenerationInput:
    """Build one bounded public-fixture request for the approved provider adapter."""

    return AnswerGenerationInput(
        research_request_id=uuid4(),
        question=question,
        sources=(source,),
    )


def _verify_answerable_proposal(
    proposal: UnvalidatedGeneratedAnswer,
    *,
    source: AnswerGenerationSource,
    support_evaluator: OpenAIStatementSupportEvaluator,
    budget: ProviderSmokeBudget,
) -> None:
    """Require every generated statement to cite and pass support against the approved passage."""

    if proposal.coverage_status is not CoverageStatus.COMPLETE or not proposal.statements:
        raise ProviderSmokeVerificationError("answerable provider smoke did not return an answer")
    if len(proposal.statements) > MAXIMUM_SUPPORT_EVALUATION_REQUESTS:
        raise ProviderSmokeVerificationError(
            "answerable provider smoke returned too many statements"
        )

    for statement in proposal.statements:
        evidence_texts: list[str] = []
        for reference in statement.evidence:
            if reference.retrieval_candidate_id != source.retrieval_candidate_id:
                raise ProviderSmokeVerificationError(
                    "answerable provider smoke cited an unknown candidate"
                )
            if reference.end_character > len(source.text) or (
                source.text[reference.start_character : reference.end_character]
                != reference.exact_text
            ):
                raise ProviderSmokeVerificationError(
                    "answerable provider smoke cited an invalid source range"
                )
            evidence_texts.append(reference.exact_text)

        budget.reserve_support(statement.text, evidence_texts)
        if not support_evaluator.supports(statement.text, evidence_texts):
            raise ProviderSmokeVerificationError(
                "answerable provider smoke statement was not independently supported"
            )


def _verify_unanswerable_proposal(proposal: UnvalidatedGeneratedAnswer) -> None:
    """Require the approved unsupported question to remain explicitly insufficient."""

    if proposal.coverage_status is not CoverageStatus.INSUFFICIENT:
        raise ProviderSmokeVerificationError(
            "unanswerable provider smoke did not remain insufficient"
        )


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
    generation_settings: GenerationProviderSettings,
) -> ProviderSmokeResult:
    """Execute one bounded compatibility request and always clean its derived namespace."""

    passage = _load_approved_passage(manifest_path)
    budget = ProviderSmokeBudget()
    budget.reserve_embedding(passage)
    providers = settings.require_indexing_providers()
    context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())
    input_id = uuid4()
    document_version_id = uuid4()
    chunk_set_id = uuid4()
    retrieval_candidate_id = uuid4()
    structure_block_id = uuid4()
    embedding_provider: OpenAIEmbeddingProvider | None = None
    writer: PineconeDenseIndexWriter | None = None
    answer_generator: OpenAIAnswerGenerator | None = None
    support_evaluator: OpenAIStatementSupportEvaluator | None = None
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
        answer_generator = create_openai_answer_generator(
            api_key=generation_settings.api_key.get_secret_value(),
            model=OPENAI_ANSWER_MODEL,
            operation_timeout_seconds=OPENAI_ANSWER_TIMEOUT_SECONDS,
            max_output_tokens=SMOKE_ANSWER_MAX_OUTPUT_TOKENS,
        )
        support_evaluator = create_openai_statement_support_evaluator(
            api_key=generation_settings.api_key.get_secret_value(),
            model=OPENAI_STATEMENT_SUPPORT_MODEL,
            operation_timeout_seconds=OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS,
            max_output_tokens=SMOKE_SUPPORT_MAX_OUTPUT_TOKENS,
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
        embedding_result = validate_embedding_response(
            request,
            embedding_provider.embed(request),
        )
        vector = build_dense_index_vector(
            context,
            chunk_id=input_id,
            document_version_id=document_version_id,
            chunk_set_id=chunk_set_id,
            embedding=embedding_result.vectors[0],
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

        source = AnswerGenerationSource(
            retrieval_candidate_id=retrieval_candidate_id,
            chunk_id=input_id,
            document_id=uuid4(),
            document_version_id=document_version_id,
            structure_block_id=structure_block_id,
            source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
            text=passage,
        )
        answerable_input = _generation_input(
            question=APPROVED_ANSWERABLE_QUESTION,
            source=source,
        )
        budget.reserve_generation(answerable_input)
        answerable_proposal = answer_generator.generate(answerable_input)
        _verify_answerable_proposal(
            answerable_proposal,
            source=source,
            support_evaluator=support_evaluator,
            budget=budget,
        )

        unanswerable_input = _generation_input(
            question=APPROVED_UNANSWERABLE_QUESTION,
            source=source,
        )
        budget.reserve_generation(unanswerable_input)
        _verify_unanswerable_proposal(answer_generator.generate(unanswerable_input))
    except Exception as error:
        primary_error = error
    finally:
        cleanup_error: Exception | None = None
        if support_evaluator is not None:
            try:
                support_evaluator.close()
            except Exception as error:
                cleanup_error = error
        if answer_generator is not None:
            try:
                answer_generator.close()
            except Exception as error:
                cleanup_error = cleanup_error or error
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
    return ProviderSmokeResult(
        embedding_requests=budget.embedding_requests,
        generation_requests=budget.generation_requests,
        support_evaluation_requests=budget.support_evaluation_requests,
        pinecone_vectors=budget.pinecone_vectors,
        estimated_maximum_spend_usd=budget.estimated_maximum_spend_usd,
    )


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
        result = verify_m1_provider_smoke(
            manifest_path=arguments.manifest,
            settings=load_worker_settings(),
            generation_settings=load_generation_provider_settings(),
        )
    except Exception:
        print("M1 provider smoke failed; provider details were withheld.", file=sys.stderr)
        return 1
    print(
        "M1 provider smoke passed: "
        f"embedding_requests={result.embedding_requests}, "
        f"generation_requests={result.generation_requests}, "
        f"support_evaluation_requests={result.support_evaluation_requests}, "
        f"pinecone_vectors={result.pinecone_vectors}, "
        f"estimated_maximum_spend_usd={result.estimated_maximum_spend_usd:.8f}, "
        "cleanup=verified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
