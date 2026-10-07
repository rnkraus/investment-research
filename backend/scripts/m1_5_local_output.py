"""Explicit, interactive-only inspection of the frozen public-synthetic evaluation."""

import json
import os
import sys
from dataclasses import dataclass
from typing import Literal

from investment_research.modules.evidence.contracts import (
    ContextualStatementSupportInput,
    StatementSupportResult,
)
from investment_research.modules.evidence.domain import ValidationRule
from investment_research.modules.research.contracts import ValidatedAnswerResult
from scripts.m1_5_live_diagnostics import ProviderFailureDiagnostic


def require_local_terminal() -> None:
    """Reject CI and redirected output before credentials, calls, or content are accessed."""

    if "CI" in os.environ or "GITHUB_ACTIONS" in os.environ or not sys.stdout.isatty():
        raise ValueError("Model output requires an interactive local terminal outside CI.")


@dataclass(slots=True, repr=False)
class LocalEvaluationOutput:
    """Write only selected fixture/model fields to the terminal, never the report or logs."""

    answer_model: str
    support_model: str
    case_id: str | None = None

    def __post_init__(self) -> None:
        require_local_terminal()

    def _emit(self, event: str, **fields: object) -> None:
        require_local_terminal()
        # JSON escapes model-authored control characters instead of executing terminal escapes.
        print(
            json.dumps(
                {"event": event, "case_id": self.case_id, **fields}, ensure_ascii=False, indent=2
            ),
            flush=True,
        )

    def begin_case(self, case_id: str, question: str) -> None:
        self.case_id = case_id
        self._emit("local.evaluation.question", question=question)

    def answer_proposal(self, output_text: str) -> None:
        self._emit(
            "local.evaluation.unvalidated_answer",
            role="answer",
            model=self.answer_model,
            notice="Unvalidated model output; citations and support are not yet approved.",
            output_text=output_text,
        )

    def support_failure(self, output_text: str) -> None:
        self._emit(
            "local.evaluation.unvalidated_support",
            role="support",
            model=self.support_model,
            notice="Unvalidated model output; not an independent quality assessment.",
            output_text=output_text,
        )

    def support_decision(
        self,
        statement: str,
        evidence_texts: tuple[str, ...],
        result: StatementSupportResult,
    ) -> None:
        self._emit(
            "local.evaluation.support_decision",
            role="support",
            model=self.support_model,
            statement=statement,
            evidence_texts=evidence_texts,
            supported=result.decision.supported,
            reason=result.diagnostic_reason,
            notice="Application support decision, not an independent quality assessment.",
        )

    def contextual_support_decision(
        self, request: ContextualStatementSupportInput, result: StatementSupportResult
    ) -> None:
        """Inspect only paired public-synthetic originals under the existing terminal guard."""

        self._emit(
            "local.evaluation.support_decision",
            role="support",
            model=self.support_model,
            statement=request.statement,
            support_policy="attribution",
            evidence=[
                {
                    "document_id": str(item.reference.document_id),
                    "document_version_id": str(item.reference.document_version_id),
                    "structure_block_id": str(item.reference.structure_block_id),
                    "exact_text": item.reference.exact_text,
                    "attribution_context": item.attribution_context.text,
                }
                for item in request.evidence
            ],
            supported=result.decision.supported,
            reason=result.diagnostic_reason,
            notice="Application support decision, not an independent quality assessment.",
        )

    def validated_result(self, result: ValidatedAnswerResult) -> None:
        self._emit(
            "local.evaluation.validated_result",
            answer_state=result.answer.state.value,
            coverage_status=result.answer.coverage_status,
            answer=result.answer.rendered_text,
            failed_validation_rules=sorted(
                {item.rule.value for item in result.validations if not item.passed}
            ),
        )

    def finish_case(
        self,
        *,
        status: Literal["COMPLETED", "FAILED"],
        diagnostic: ProviderFailureDiagnostic | None,
        failed_rules: tuple[ValidationRule, ...],
    ) -> None:
        self._emit(
            "local.evaluation.case_finished",
            status=status,
            provider_failure=None if diagnostic is None else diagnostic.model_dump(mode="json"),
            failed_validation_rules=[rule.value for rule in failed_rules],
        )
