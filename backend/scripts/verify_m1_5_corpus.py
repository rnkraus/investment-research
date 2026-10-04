"""Validate the versioned, public-synthetic M1.5 evaluation corpus offline."""

import argparse
import hashlib
import sys
from io import BytesIO
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from investment_research.infrastructure.parsing.pypdf import PypdfDocumentParser
from investment_research.modules.processing.contracts import ParserLimits

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPOSITORY_ROOT / "fixtures/m1_5/evaluation.json"
PDF_LIMITS = ParserLimits(max_pages=10, max_extracted_characters=100_000)


class CorpusValidationError(Exception):
    """Report a catalog or fixture defect without printing source content."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class SourceManifest(_FrozenModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Document(_FrozenModel):
    id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    media_type: Literal["application/pdf", "text/plain"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license: Literal["CC0-1.0"]
    provenance: str | None = None


class SourceAnchor(_FrozenModel):
    kind: Literal["PDF_PAGE", "TEXT_PASSAGE"]
    start_character: int = Field(ge=0)
    end_character: int = Field(gt=0)
    page_number: int | None = Field(default=None, gt=0)


class Passage(_FrozenModel):
    id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    source_anchor: SourceAnchor


class Case(_FrozenModel):
    id: str = Field(min_length=1)
    categories: tuple[str, ...] = Field(min_length=1)
    question: str = Field(min_length=1)
    scope_document_ids: tuple[str, ...] = Field(min_length=1)
    expected_coverage: Literal["COMPLETE", "PARTIAL", "CONFLICTING", "INSUFFICIENT"]
    expected_answer: str | None
    evidence_groups: tuple[tuple[str, ...], ...]


class EvaluationManifest(_FrozenModel):
    schema_version: Literal[1]
    id: str = Field(min_length=1)
    review_status: Literal["pending_owner_review", "reviewed"]
    language: Literal["en"]
    data_classification: Literal["public_synthetic"]
    external_provider_use: Literal["explicit_opt_in_only"]
    source_manifest: SourceManifest
    documents: tuple[Document, ...] = Field(min_length=1)
    passages: tuple[Passage, ...] = Field(min_length=1)
    cases: tuple[Case, ...] = Field(min_length=1)


def _fixture_bytes(repository_root: Path, relative_path: str, expected_sha256: str) -> bytes:
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or relative.parts[0] != "fixtures":
        raise CorpusValidationError("a fixture path is outside the approved corpus directory")
    path = (repository_root / relative).resolve()
    if not path.is_relative_to((repository_root / "fixtures").resolve()) or not path.is_file():
        raise CorpusValidationError("a fixture path is missing or escapes the approved corpus")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise CorpusValidationError("a fixture digest does not match the catalog")
    return data


def _unique_ids(values: tuple[Document, ...] | tuple[Passage, ...] | tuple[Case, ...]) -> bool:
    return len({value.id for value in values}) == len(values)


def validate_corpus(manifest_path: Path, repository_root: Path) -> EvaluationManifest:
    """Check schema, immutable inputs, original anchors, and expected-case references."""

    try:
        manifest = EvaluationManifest.model_validate_json(manifest_path.read_bytes())
    except (OSError, ValidationError) as error:
        raise CorpusValidationError(
            "the evaluation catalog is missing or has an invalid schema"
        ) from error

    if not all(
        _unique_ids(values) for values in (manifest.documents, manifest.passages, manifest.cases)
    ):
        raise CorpusValidationError("the evaluation catalog contains duplicate IDs")

    _fixture_bytes(
        repository_root,
        manifest.source_manifest.path,
        manifest.source_manifest.sha256,
    )
    document_pages: dict[str, tuple[str, ...]] = {}
    document_media: dict[str, str] = {}
    for document in manifest.documents:
        data = _fixture_bytes(repository_root, document.path, document.sha256)
        document_media[document.id] = document.media_type
        if document.media_type == "application/pdf":
            parsed = PypdfDocumentParser().parse(BytesIO(data), limits=PDF_LIMITS)
            document_pages[document.id] = tuple(page.text for page in parsed.pages)
        else:
            try:
                text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
            except UnicodeDecodeError as error:
                raise CorpusValidationError("a text fixture is not valid UTF-8") from error
            document_pages[document.id] = (text,)

    passages_by_id = {passage.id: passage for passage in manifest.passages}
    for passage in manifest.passages:
        if passage.document_id not in document_pages:
            raise CorpusValidationError("a passage references an unknown document")
        anchor = passage.source_anchor
        media_type = document_media[passage.document_id]
        if (media_type, anchor.kind) not in {
            ("application/pdf", "PDF_PAGE"),
            ("text/plain", "TEXT_PASSAGE"),
        }:
            raise CorpusValidationError("a passage has the wrong source-anchor kind")
        if anchor.kind == "PDF_PAGE":
            if anchor.page_number is None or anchor.page_number > len(
                document_pages[passage.document_id]
            ):
                raise CorpusValidationError("a PDF page anchor is invalid")
            source_text = document_pages[passage.document_id][anchor.page_number - 1]
        else:
            if anchor.page_number is not None:
                raise CorpusValidationError("a text anchor cannot have a page number")
            source_text = document_pages[passage.document_id][0]
        if (
            anchor.start_character >= anchor.end_character
            or source_text[anchor.start_character : anchor.end_character] != passage.text
        ):
            raise CorpusValidationError("a passage does not match its original source range")

    for case in manifest.cases:
        scope = set(case.scope_document_ids)
        if len(scope) != len(case.scope_document_ids) or not scope <= document_pages.keys():
            raise CorpusValidationError("a case has an invalid document scope")
        if case.expected_coverage == "INSUFFICIENT":
            if case.evidence_groups or case.expected_answer is not None:
                raise CorpusValidationError(
                    "an insufficient case must expect no answer or evidence"
                )
        elif not case.evidence_groups or not case.expected_answer:
            raise CorpusValidationError("an answerable case needs an answer and evidence")
        evidence_documents: set[str] = set()
        for group in case.evidence_groups:
            if not group:
                raise CorpusValidationError("an evidence group has no alternatives")
            for passage_id in group:
                evidence_passage = passages_by_id.get(passage_id)
                if evidence_passage is None or evidence_passage.document_id not in scope:
                    raise CorpusValidationError("a case references evidence outside its scope")
                evidence_documents.add(evidence_passage.document_id)
        if case.expected_coverage == "CONFLICTING" and len(evidence_documents) < 2:
            raise CorpusValidationError("a conflict case must cite distinct documents")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--require-reviewed", action="store_true")
    arguments = parser.parse_args()
    try:
        manifest = validate_corpus(arguments.manifest, REPOSITORY_ROOT)
        if arguments.require_reviewed and manifest.review_status != "reviewed":
            raise CorpusValidationError("the evaluation catalog awaits owner review")
    except CorpusValidationError as error:
        print(f"M1.5 corpus verification failed: {error}", file=sys.stderr)
        return 1
    print(
        f"M1.5 corpus verified: {len(manifest.documents)} documents, "
        f"{len(manifest.passages)} passages, {len(manifest.cases)} cases; "
        f"review_status={manifest.review_status}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
