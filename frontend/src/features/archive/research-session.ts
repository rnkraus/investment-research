import type { EvidenceSpanResult, ResearchStreamEvent } from "@/lib/api/client";

export type ResearchSessionStatus =
  "IDLE" | "RUNNING" | "COMPLETED" | "INSUFFICIENT" | "FAILED";

export type ResearchProgressStage = Extract<
  ResearchStreamEvent,
  { readonly event: "research.progress" }
>["stage"];

export const RESEARCH_PROGRESS_ORDER: readonly ResearchProgressStage[] = [
  "EMBEDDING_QUESTION",
  "SEARCHING_SOURCES",
  "SELECTING_PASSAGES",
  "GENERATING_ANSWER",
  "VALIDATING_EVIDENCE",
  "SAVING_RESULT",
];

export type ResearchDisplayStage =
  | "PREPARING_QUESTION"
  | "FINDING_PASSAGES"
  | "DRAFTING_ANSWER"
  | "CHECKING_EVIDENCE";

const DISPLAY_STAGE_BY_PROGRESS_STAGE: Record<
  ResearchProgressStage,
  ResearchDisplayStage
> = {
  EMBEDDING_QUESTION: "PREPARING_QUESTION",
  SEARCHING_SOURCES: "FINDING_PASSAGES",
  SELECTING_PASSAGES: "FINDING_PASSAGES",
  GENERATING_ANSWER: "DRAFTING_ANSWER",
  VALIDATING_EVIDENCE: "CHECKING_EVIDENCE",
  SAVING_RESULT: "CHECKING_EVIDENCE",
};

const RESEARCH_DISPLAY_ORDER: readonly ResearchDisplayStage[] = [
  "PREPARING_QUESTION",
  "FINDING_PASSAGES",
  "DRAFTING_ANSWER",
  "CHECKING_EVIDENCE",
];

export interface ResearchDisplayStep {
  readonly stage: ResearchDisplayStage;
  readonly status: "PENDING" | "ACTIVE" | "COMPLETED";
}

export interface ResearchCitation {
  readonly evidenceSpanId: string;
  readonly relationship: "SUPPORTS" | "CONFLICTS";
  readonly displayOrder: number;
  readonly source?: EvidenceSpanResult;
}

export interface ResearchStatement {
  readonly statementId: string;
  readonly order: number;
  readonly text: string;
  readonly citations: readonly ResearchCitation[];
}

export interface ResearchSession {
  readonly status: ResearchSessionStatus;
  readonly researchRequestId?: string;
  readonly stage?: ResearchProgressStage;
  readonly progressStages: readonly ResearchProgressStage[];
  readonly coverage?: "COMPLETE" | "PARTIAL" | "CONFLICTING";
  readonly message?: string;
  readonly statements: readonly ResearchStatement[];
}

export class ResearchSequenceError extends Error {
  constructor(message = "The research stream has an invalid event sequence.") {
    super(message);
    this.name = "ResearchSequenceError";
  }
}

export const EMPTY_RESEARCH_SESSION: ResearchSession = {
  status: "IDLE",
  progressStages: [],
  statements: [],
};

export function beginResearchSession(): ResearchSession {
  return {
    status: "RUNNING",
    progressStages: [],
    statements: [],
  };
}

export function getResearchDisplaySteps(
  session: ResearchSession,
): readonly ResearchDisplayStep[] {
  const visibleOrder =
    session.progressStages.length > 0 &&
    session.progressStages[0] !== "EMBEDDING_QUESTION"
      ? RESEARCH_DISPLAY_ORDER.slice(1)
      : RESEARCH_DISPLAY_ORDER;
  const observedStages = new Set(
    session.progressStages.map(
      (stage) => DISPLAY_STAGE_BY_PROGRESS_STAGE[stage],
    ),
  );
  const activeStage =
    session.stage === undefined
      ? undefined
      : DISPLAY_STAGE_BY_PROGRESS_STAGE[session.stage];

  return visibleOrder.map((stage) => ({
    stage,
    status:
      stage === activeStage
        ? "ACTIVE"
        : observedStages.has(stage)
          ? "COMPLETED"
          : "PENDING",
  }));
}

function requireRunningSession(session: ResearchSession): void {
  if (session.status !== "RUNNING") {
    throw new ResearchSequenceError();
  }
}

function requireConsistentRequest(
  session: ResearchSession,
  event: ResearchStreamEvent,
): string {
  if (
    session.researchRequestId !== undefined &&
    session.researchRequestId !== event.research_request_id
  ) {
    throw new ResearchSequenceError();
  }
  return event.research_request_id;
}

function addStatement(
  session: ResearchSession,
  event: Extract<ResearchStreamEvent, { readonly event: "answer.statement" }>,
  researchRequestId: string,
): ResearchSession {
  if (
    session.statements.some(
      (statement) =>
        statement.statementId === event.statement_id ||
        statement.order === event.order,
    )
  ) {
    throw new ResearchSequenceError();
  }

  return {
    ...session,
    researchRequestId,
    stage: undefined,
    statements: [
      ...session.statements,
      {
        statementId: event.statement_id,
        order: event.order,
        text: event.text,
        citations: [],
      },
    ].sort((left, right) => left.order - right.order),
  };
}

function addCitation(
  session: ResearchSession,
  event: Extract<ResearchStreamEvent, { readonly event: "answer.citation" }>,
  researchRequestId: string,
): ResearchSession {
  let foundStatement = false;
  const statements = session.statements.map((statement) => {
    if (statement.statementId !== event.statement_id) {
      return statement;
    }
    foundStatement = true;
    if (
      statement.citations.some(
        (citation) =>
          citation.evidenceSpanId === event.evidence_span_id ||
          citation.displayOrder === event.display_order,
      )
    ) {
      throw new ResearchSequenceError();
    }
    return {
      ...statement,
      citations: [
        ...statement.citations,
        {
          evidenceSpanId: event.evidence_span_id,
          relationship: event.relationship,
          displayOrder: event.display_order,
        },
      ].sort((left, right) => left.displayOrder - right.displayOrder),
    };
  });

  if (!foundStatement) {
    throw new ResearchSequenceError();
  }
  return { ...session, researchRequestId, statements };
}

export function applyResearchEvent(
  session: ResearchSession,
  event: ResearchStreamEvent,
): ResearchSession {
  requireRunningSession(session);
  const researchRequestId = requireConsistentRequest(session, event);

  switch (event.event) {
    case "research.progress":
      if (session.statements.length > 0) {
        throw new ResearchSequenceError();
      }
      if (
        session.stage !== undefined &&
        RESEARCH_PROGRESS_ORDER.indexOf(event.stage) <=
          RESEARCH_PROGRESS_ORDER.indexOf(session.stage)
      ) {
        throw new ResearchSequenceError();
      }
      return {
        ...session,
        researchRequestId,
        stage: event.stage,
        progressStages: [...session.progressStages, event.stage],
      };
    case "answer.statement":
      return addStatement(session, event, researchRequestId);
    case "answer.citation":
      return addCitation(session, event, researchRequestId);
    case "answer.completed":
      if (
        event.coverage === "INSUFFICIENT" ||
        session.statements.length === 0 ||
        session.statements.some((statement) => statement.citations.length === 0)
      ) {
        throw new ResearchSequenceError();
      }
      return {
        ...session,
        status: "COMPLETED",
        researchRequestId,
        stage: undefined,
        coverage: event.coverage,
      };
    case "answer.insufficient":
      if (session.statements.length > 0) {
        throw new ResearchSequenceError();
      }
      return {
        ...session,
        status: "INSUFFICIENT",
        researchRequestId,
        stage: undefined,
        message: event.message,
      };
    case "research.failed":
      if (session.statements.length > 0) {
        throw new ResearchSequenceError();
      }
      return {
        ...session,
        status: "FAILED",
        researchRequestId,
        stage: undefined,
        message: event.error.message,
      };
  }
}

export function attachCitationSource(
  session: ResearchSession,
  source: EvidenceSpanResult,
): ResearchSession {
  if (session.status !== "COMPLETED") {
    throw new ResearchSequenceError();
  }

  let foundCitation = false;
  const statements = session.statements.map((statement) => ({
    ...statement,
    citations: statement.citations.map((citation) => {
      if (citation.evidenceSpanId !== source.evidence_span_id) {
        return citation;
      }
      foundCitation = true;
      return { ...citation, source };
    }),
  }));

  if (!foundCitation) {
    throw new ResearchSequenceError();
  }
  return { ...session, statements };
}
