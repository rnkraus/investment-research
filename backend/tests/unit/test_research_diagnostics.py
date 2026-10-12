"""Offline checks for narrowly scoped, unvalidated rejection diagnostics."""

from uuid import UUID

import pytest

from investment_research.api.research_diagnostics import build_rejection_diagnostic
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.evidence.contracts import (
    EvidenceValidationDecision,
    ValidationFinding,
)
from investment_research.modules.evidence.domain import ValidationRule
from investment_research.modules.research.contracts import (
    AnswerGenerationSource,
    ResearchModel,
    UnvalidatedEvidenceReference,
    UnvalidatedGeneratedAnswer,
    UnvalidatedGeneratedStatement,
)
from investment_research.modules.research.domain import CoverageStatus


@pytest.mark.parametrize("reference_kind", ["matching", "unknown", "mismatch", "out_of_range"])
def test_diagnostic_exposes_only_failed_claims_and_matching_selected_ranges(
    reference_kind: str,
) -> None:
    text = "Café 😀 supplies computing capacity."
    source = AnswerGenerationSource(
        retrieval_candidate_id=UUID(int=1),
        chunk_id=UUID(int=2),
        document_id=UUID(int=3),
        document_version_id=UUID(int=4),
        structure_block_id=UUID(int=5),
        source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
        text=text,
    )
    quote = "😀" if reference_kind != "mismatch" else "X"
    start = len(text) + 1 if reference_kind == "out_of_range" else text.index("😀")
    reference = UnvalidatedEvidenceReference(
        retrieval_candidate_id=UUID(int=99)
        if reference_kind == "unknown"
        else source.retrieval_candidate_id,
        start_character=start,
        end_character=start + len(quote),
        exact_text=quote,
    )
    claim = UnvalidatedGeneratedStatement(text="A rejected claim.", evidence=(reference,))
    proposal = UnvalidatedGeneratedAnswer(
        coverage_status=CoverageStatus.COMPLETE,
        rendered_text="Do not disclose this rendering.",
        statements=(claim, claim.model_copy(update={"text": "A different, passing claim."})),
    )
    decision = EvidenceValidationDecision(
        accepted=False,
        coverage_status=None,
        statements=(),
        findings=(
            ValidationFinding(
                rule=ValidationRule.STATEMENT_SUPPORT,
                statement_ordinal=0,
                passed=False,
                reason="Claim is not fully supported.",
            ),
            ValidationFinding(
                rule=ValidationRule.STATEMENT_SUPPORT, statement_ordinal=1, passed=True
            ),
        ),
    )
    diagnostic = build_rejection_diagnostic(
        proposal,
        decision,
        (source,),
        answer_model=ResearchModel.TERRA,
        support_model=ResearchModel.LUNA,
    )
    assert diagnostic is not None and diagnostic.unvalidated is True
    assert len(diagnostic.statements) == len(diagnostic.findings) == 1
    assert diagnostic.statements[0].text == claim.text
    assert len(diagnostic.statements[0].evidence) == (1 if reference_kind == "matching" else 0)
    assert "rendered_text" not in diagnostic.model_dump_json()
    assert "exact_text" not in diagnostic.model_dump_json()
    assert "A different, passing claim." not in diagnostic.model_dump_json()
