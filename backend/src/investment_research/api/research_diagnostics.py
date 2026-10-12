"""Build opt-in local rejection details without disclosing additional source content."""

from investment_research.api.contracts import (
    RejectedEvidenceRange,
    RejectedFindingDiagnostic,
    RejectedStatementDiagnostic,
    ResearchRejectionDiagnostic,
)
from investment_research.modules.evidence.contracts import EvidenceValidationDecision
from investment_research.modules.research.contracts import (
    AnswerGenerationSource,
    ResearchModel,
    UnvalidatedGeneratedAnswer,
)


def build_rejection_diagnostic(
    proposal: UnvalidatedGeneratedAnswer,
    decision: EvidenceValidationDecision,
    sources: tuple[AnswerGenerationSource, ...],
    *,
    answer_model: ResearchModel,
    support_model: ResearchModel,
) -> ResearchRejectionDiagnostic | None:
    """Show only failed claims and matching ranges in this request's selected context."""

    failed = tuple(finding for finding in decision.findings if not finding.passed)
    if decision.accepted or not failed:
        return None
    rejected = {finding.statement_ordinal for finding in failed}
    selected = {source.retrieval_candidate_id: source for source in sources}
    statements = []
    for ordinal, statement in enumerate(proposal.statements):
        if ordinal not in rejected:
            continue
        ranges = []
        for reference in statement.evidence:
            source = selected.get(reference.retrieval_candidate_id)
            if source is None or reference.end_character > len(source.text):
                continue
            if (
                source.text[reference.start_character : reference.end_character]
                != reference.exact_text
            ):
                continue
            ranges.append(
                RejectedEvidenceRange(
                    retrieval_candidate_id=reference.retrieval_candidate_id,
                    start_character=reference.start_character,
                    end_character=reference.end_character,
                )
            )
        statements.append(
            RejectedStatementDiagnostic(
                ordinal=ordinal, text=statement.text, evidence=tuple(ranges)
            )
        )
    return ResearchRejectionDiagnostic(
        answer_model=answer_model,
        support_model=support_model,
        statements=tuple(statements),
        findings=tuple(
            RejectedFindingDiagnostic(
                rule=finding.rule,
                statement_ordinal=finding.statement_ordinal,
                reason=finding.reason or "Evidence validation rejected this proposal.",
            )
            for finding in failed
        ),
    )
