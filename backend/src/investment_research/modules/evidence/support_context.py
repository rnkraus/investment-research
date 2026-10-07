"""Prepare bounded original attribution context without provider or database calls."""

import re
from collections.abc import Mapping, Sequence
from hashlib import sha256
from uuid import UUID

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.evidence.application import ValidateGeneratedAnswerService
from investment_research.modules.evidence.contracts import (
    M1_5_DOCUMENT_ATTRIBUTION_POLICY_VERSION,
    M1_5_SUPPORT_CONTEXT_CHARACTER_LIMIT,
    CanonicalEvidenceSource,
    ContextualStatementEvidence,
    ContextualStatementSupportInput,
    OriginalSupportContext,
    ValidatedEvidenceReference,
)
from investment_research.modules.identity.domain import WorkspaceContext

_PARAGRAPH_SEPARATOR = re.compile(r"\n[ \t]*\n")


def build_contextual_support_input(
    *,
    context: WorkspaceContext,
    retrieval_run_id: UUID,
    statement: str,
    references: Sequence[ValidatedEvidenceReference],
    sources: Mapping[UUID, CanonicalEvidenceSource | None],
) -> ContextualStatementSupportInput | None:
    """Pair eligible exact quotes with a bounded prefix of their own original block.

    Return no input when any source or reference is unavailable or ineligible. Typed input
    limits raise a content-free validation error before a caller can contact a provider.
    The prefix is attribution context, not an extracted or trusted company/date assertion.
    """

    evidence: list[ContextualStatementEvidence] = []
    for reference in references:
        source = sources.get(reference.retrieval_candidate_id)
        if (
            source is None
            or source.retrieval_candidate_id != reference.retrieval_candidate_id
            or not ValidateGeneratedAnswerService.is_source_eligible(
                context, retrieval_run_id, source
            )
            or not _matches_original_reference(reference, source)
        ):
            return None
        start, end = original_attribution_prefix_range(source.structure_block_text)
        evidence.append(
            ContextualStatementEvidence(
                reference=reference,
                attribution_context=OriginalSupportContext(
                    document_id=source.document_id,
                    document_version_id=source.document_version_id,
                    structure_block_id=source.structure_block_id,
                    start_character=start,
                    end_character=end,
                    text=source.structure_block_text[start:end],
                ),
            )
        )
    return ContextualStatementSupportInput(statement=statement, evidence=tuple(evidence))


def build_document_contextual_support_input(
    *,
    context: WorkspaceContext,
    retrieval_run_id: UUID,
    statement: str,
    references: Sequence[ValidatedEvidenceReference],
    sources: Mapping[UUID, CanonicalEvidenceSource | None],
) -> ContextualStatementSupportInput | None:
    """Use only a canonical first-block prefix from the same workspace/version/processing run.

    A missing prefix does not silently fall back to another policy or a guessed report identity.
    The reader supplies an exact original range; final persistence reloads it independently.
    """

    evidence = []
    for reference in references:
        source = sources.get(reference.retrieval_candidate_id)
        if (
            source is None
            or source.retrieval_candidate_id != reference.retrieval_candidate_id
            or not ValidateGeneratedAnswerService.is_source_eligible(
                context, retrieval_run_id, source
            )
            or not _matches_original_reference(reference, source)
            or source.document_attribution_context is None
        ):
            return None
        attribution = OriginalSupportContext.model_validate(
            source.document_attribution_context.model_dump()
        )
        if (
            attribution.policy_version != M1_5_DOCUMENT_ATTRIBUTION_POLICY_VERSION
            or attribution.workspace_id != context.workspace_id
            or attribution.processing_run_id != source.processing_run_id
            or attribution.document_id != source.document_id
            or attribution.document_version_id != source.document_version_id
        ):
            return None
        evidence.append(
            ContextualStatementEvidence(reference=reference, attribution_context=attribution)
        )
    return ContextualStatementSupportInput(statement=statement, evidence=tuple(evidence))


def original_attribution_prefix_range(text: str) -> tuple[int, int]:
    """Select a bounded prefix by moving offsets, never by rewriting original text."""

    start = 0
    while start < len(text) and text[start].isspace():
        start += 1
    separator = _PARAGRAPH_SEPARATOR.search(text, start)
    end = min(
        len(text) if separator is None else separator.start(),
        start + M1_5_SUPPORT_CONTEXT_CHARACTER_LIMIT,
    )
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _matches_original_reference(
    reference: ValidatedEvidenceReference,
    source: CanonicalEvidenceSource,
) -> bool:
    """Recheck identity, exact text, hash, and media-specific original coordinates."""

    if (
        source.structure_block_start_character < 0
        or source.structure_block_end_character - source.structure_block_start_character
        != len(source.structure_block_text)
    ):
        return False
    if (
        reference.document_id,
        reference.document_version_id,
        reference.structure_block_id,
        reference.source_locator_kind,
        reference.page_number,
    ) != (
        source.document_id,
        source.document_version_id,
        source.structure_block_id,
        source.source_locator_kind,
        source.page_number,
    ):
        return False
    if (
        not 0
        <= reference.start_character
        < reference.end_character
        <= len(source.structure_block_text)
    ):
        return False
    if (
        source.structure_block_text[reference.start_character : reference.end_character]
        != reference.exact_text
        or sha256(reference.exact_text.encode("utf-8")).hexdigest() != reference.text_sha256
    ):
        return False
    if source.source_locator_kind is SourceLocatorKind.TEXT_PASSAGE:
        return (
            reference.source_start_character
            == source.structure_block_start_character + reference.start_character
            and reference.source_end_character
            == source.structure_block_start_character + reference.end_character
        )
    return reference.source_start_character is None and reference.source_end_character is None
