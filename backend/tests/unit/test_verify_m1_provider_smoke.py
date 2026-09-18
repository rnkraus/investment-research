"""Unit tests for the explicit, bounded M1 live-provider smoke runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from investment_research.infrastructure.search.openai import OpenAIEmbeddingProvider
from investment_research.infrastructure.search.pinecone import PineconeDenseIndexWriter
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseVectorId,
    EmbeddingRequest,
    EmbeddingVector,
)
from investment_research.settings import WorkerSettings
from scripts import verify_m1_provider_smoke as smoke_module
from scripts.verify_m1_provider_smoke import (
    APPROVED_MANIFEST,
    MAXIMUM_TOTAL_SPEND_USD,
    ProviderSmokeVerificationError,
    verify_m1_provider_smoke,
)


@dataclass(slots=True)
class RecordingEmbeddingProvider:
    """Return one contract-valid vector or one private synthetic failure."""

    fail: bool = False
    calls: list[EmbeddingRequest] = field(default_factory=list)
    closed: bool = False

    def embed(self, request: EmbeddingRequest) -> tuple[EmbeddingVector, ...]:
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("sensitive embedding failure")
        return tuple(
            EmbeddingVector(
                input_id=item.input_id,
                values=(0.125,) * M1_EMBEDDING_DIMENSIONS,
                embedding_config_version=request.embedding_config_version,
            )
            for item in request.inputs
        )

    def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class RecordingIndex:
    """Record cleanup and make the test writer's vector disappear."""

    writer: RecordingDenseWriter
    delete_calls: list[dict[str, object]] = field(default_factory=list)

    def delete(self, **kwargs: object) -> None:
        self.delete_calls.append(kwargs)
        self.writer.deleted = True


@dataclass(slots=True)
class RecordingDenseWriter:
    """Acknowledge one vector and expose it until namespace cleanup."""

    operation_timeout_seconds: float = 20.0
    deleted: bool = False
    closed: bool = False
    fail_close: bool = False
    upsert_calls: list[tuple[WorkspaceContext, DenseIndexWriteRequest]] = field(
        default_factory=list
    )
    fetch_calls: list[tuple[WorkspaceContext, tuple[DenseVectorId, ...]]] = field(
        default_factory=list
    )
    index: RecordingIndex = field(init=False)

    def __post_init__(self) -> None:
        self.index = RecordingIndex(self)

    def upsert(
        self,
        context: WorkspaceContext,
        request: DenseIndexWriteRequest,
    ) -> DenseIndexWriteResult:
        self.upsert_calls.append((context, request))
        return DenseIndexWriteResult(
            vector_ids=tuple(vector.vector_id for vector in request.vectors)
        )

    def fetch_visible(
        self,
        context: WorkspaceContext,
        vector_ids: tuple[DenseVectorId, ...],
    ) -> tuple[DenseVectorId, ...]:
        self.fetch_calls.append((context, vector_ids))
        return () if self.deleted else vector_ids

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("sensitive close failure")


def make_settings() -> WorkerSettings:
    """Build only the synthetic provider settings needed by the smoke runner."""

    return WorkerSettings.model_validate(
        {
            "environment": "test",
            "indexing_providers": {
                "embedding": {"api_key": "synthetic-openai-key"},
                "pinecone": {
                    "api_key": "synthetic-pinecone-key",
                    "index_host": "synthetic-index.example.svc.pinecone.io",
                },
            },
        }
    )


def install_provider_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding: RecordingEmbeddingProvider,
    writer: RecordingDenseWriter,
) -> None:
    """Replace both provider factories without changing the application contracts."""

    monkeypatch.setattr(
        smoke_module,
        "create_openai_embedding_provider",
        lambda **_: cast(OpenAIEmbeddingProvider, embedding),
    )
    monkeypatch.setattr(
        smoke_module,
        "create_pinecone_dense_index_writer",
        lambda **_: cast(PineconeDenseIndexWriter, writer),
    )


def test_smoke_uses_one_bounded_vector_and_verifies_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding = RecordingEmbeddingProvider()
    writer = RecordingDenseWriter()
    install_provider_fakes(monkeypatch, embedding=embedding, writer=writer)

    estimated_spend = verify_m1_provider_smoke(
        manifest_path=APPROVED_MANIFEST,
        settings=make_settings(),
    )

    assert Decimal("0") < estimated_spend < MAXIMUM_TOTAL_SPEND_USD
    assert len(embedding.calls) == 1
    assert len(embedding.calls[0].inputs) == 1
    assert len(writer.upsert_calls) == 1
    assert len(writer.upsert_calls[0][1].vectors) == 1
    assert len(writer.index.delete_calls) == 1
    assert writer.index.delete_calls[0]["delete_all"] is True
    assert writer.index.delete_calls[0]["namespace"]
    assert writer.deleted is True
    assert writer.closed is True
    assert embedding.closed is True


def test_smoke_rejects_unapproved_manifest_before_provider_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    constructor_calls = 0

    def record_constructor(**_: object) -> OpenAIEmbeddingProvider:
        nonlocal constructor_calls
        constructor_calls += 1
        raise AssertionError("provider construction must not be reached")

    copied_manifest = tmp_path / "manifest.yaml"
    copied_manifest.write_bytes(APPROVED_MANIFEST.read_bytes())
    monkeypatch.setattr(smoke_module, "create_openai_embedding_provider", record_constructor)

    with pytest.raises(ProviderSmokeVerificationError, match="approved M1 manifest"):
        verify_m1_provider_smoke(
            manifest_path=copied_manifest,
            settings=make_settings(),
        )

    assert constructor_calls == 0


def test_provider_failure_is_safe_and_still_cleans_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding = RecordingEmbeddingProvider(fail=True)
    writer = RecordingDenseWriter()
    install_provider_fakes(monkeypatch, embedding=embedding, writer=writer)

    with pytest.raises(ProviderSmokeVerificationError, match="failed safely") as captured:
        verify_m1_provider_smoke(
            manifest_path=APPROVED_MANIFEST,
            settings=make_settings(),
        )

    assert "sensitive" not in str(captured.value)
    assert writer.index.delete_calls[0]["delete_all"] is True
    assert writer.closed is True
    assert embedding.closed is True


def test_cleanup_failure_is_safe_and_does_not_skip_other_client_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding = RecordingEmbeddingProvider()
    writer = RecordingDenseWriter(fail_close=True)
    install_provider_fakes(monkeypatch, embedding=embedding, writer=writer)

    with pytest.raises(ProviderSmokeVerificationError, match="cleanup failed") as captured:
        verify_m1_provider_smoke(
            manifest_path=APPROVED_MANIFEST,
            settings=make_settings(),
        )

    assert "sensitive" not in str(captured.value)
    assert writer.deleted is True
    assert writer.closed is True
    assert embedding.closed is True
