import type { EvidenceSpanResult, ResearchStreamEvent } from "@/lib/api/client";

export type ResearchSessionStatus =
  "IDLE" | "RUNNING" | "COMPLETED" | "INSUFFICIENT" | "FAILED";

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
  readonly stage?: "RETRIEVING" | "GENERATING" | "VALIDATING";
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
  statements: [],
};

export function beginResearchSession(): ResearchSession {
  return {
    status: "RUNNING",
    statements: [],
  };
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
      return { ...session, researchRequestId, stage: event.stage };
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
