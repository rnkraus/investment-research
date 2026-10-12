import assert from "node:assert/strict";
import test from "node:test";

import {
  ResearchSequenceError,
  applyResearchEvent,
  attachCitationSource,
  beginResearchSession,
  getResearchDisplaySteps,
} from "../src/features/archive/research-session.ts";

const REQUEST_ID = "10000000-0000-4000-8000-000000000001";
const OTHER_REQUEST_ID = "10000000-0000-4000-8000-000000000002";
const STATEMENT_ID = "10000000-0000-4000-8000-000000000003";
const SPAN_ID = "10000000-0000-7000-8000-000000000004";
const DOCUMENT_ID = "10000000-0000-4000-8000-000000000005";
const VERSION_ID = "10000000-0000-4000-8000-000000000006";

function statementEvent(requestId = REQUEST_ID) {
  return {
    event: "answer.statement",
    schema_version: "1.0",
    research_request_id: requestId,
    statement_id: STATEMENT_ID,
    order: 0,
    text: "Revenue increased by 22.0%.",
  };
}

function citationEvent(requestId = REQUEST_ID) {
  return {
    event: "answer.citation",
    schema_version: "1.0",
    research_request_id: requestId,
    statement_id: STATEMENT_ID,
    evidence_span_id: SPAN_ID,
    relationship: "SUPPORTS",
    display_order: 0,
  };
}

function completedEvent(requestId = REQUEST_ID, coverage = "COMPLETE") {
  return {
    event: "answer.completed",
    schema_version: "1.0",
    research_request_id: requestId,
    coverage,
  };
}

function failedEvent(requestId = REQUEST_ID) {
  return {
    event: "research.failed",
    schema_version: "1.0",
    research_request_id: requestId,
    error: {
      code: "PROVIDER_UNAVAILABLE",
      message: "The research provider is temporarily unavailable.",
      request_id: requestId,
    },
    retriable: true,
  };
}

function selectedContextEvent(requestId = REQUEST_ID) {
  return {
    event: "research.context",
    schema_version: "1.0",
    research_request_id: requestId,
    sources: [
      {
        retrieval_candidate_id: STATEMENT_ID,
        chunk_id: SPAN_ID,
        document_id: DOCUMENT_ID,
        document_version_id: VERSION_ID,
        structure_block_id: OTHER_REQUEST_ID,
        source_locator_kind: "TEXT_PASSAGE",
        page_number: null,
        text: "Revenue increased by 22.0%.\nUnchanged source text.",
      },
    ],
  };
}

function rejectedAnswerEvent() {
  return {
    ...failedEvent(),
    error: {
      code: "EVIDENCE_VALIDATION_FAILED",
      message: "Answer failed evidence validation.",
      request_id: REQUEST_ID,
    },
    retriable: false,
    diagnostic: {
      unvalidated: true,
      answer_model: "gpt-5.6-terra",
      support_model: "gpt-5.6-luna",
      statements: [
        {
          ordinal: 0,
          text: "An unvalidated claim.",
          evidence: [
            {
              retrieval_candidate_id: STATEMENT_ID,
              start_character: 0,
              end_character: 25,
            },
          ],
        },
      ],
      findings: [
        {
          rule: "STATEMENT_SUPPORT",
          statement_ordinal: 0,
          reason: "Canonical evidence does not support the complete statement.",
        },
      ],
    },
  };
}

test("rejected claims remain diagnostics rather than accepted statements and reset for a new question", () => {
  const selected = applyResearchEvent(
    beginResearchSession(),
    selectedContextEvent(),
  );
  const event = rejectedAnswerEvent();
  const rejected = applyResearchEvent(selected, event);
  assert.equal(rejected.status, "FAILED");
  assert.deepEqual(rejected.rejectionDiagnostic, event.diagnostic);
  assert.deepEqual(rejected.statements, []);
  assert.deepEqual(rejected.retrievalContext, selected.retrievalContext);
  assert.equal(beginResearchSession().rejectionDiagnostic, undefined);
});

test("rejection diagnostics require this request's selected context and valid Unicode ranges", () => {
  const event = rejectedAnswerEvent();
  assert.throws(
    () => applyResearchEvent(beginResearchSession(), event),
    ResearchSequenceError,
  );
  const context = selectedContextEvent();
  context.sources[0].text = "Café 😀 supplies computing capacity.";
  const selected = applyResearchEvent(beginResearchSession(), context);
  const statement = event.diagnostic.statements[0];
  const range = statement.evidence[0];
  for (const invalid of [
    { ...range, retrieval_candidate_id: OTHER_REQUEST_ID },
    { ...range, start_character: -1 },
    { ...range, end_character: 0 },
    { ...range, end_character: Array.from(context.sources[0].text).length + 1 },
  ]) {
    assert.throws(
      () =>
        applyResearchEvent(selected, {
          ...event,
          diagnostic: {
            ...event.diagnostic,
            statements: [{ ...statement, evidence: [invalid] }],
          },
        }),
      ResearchSequenceError,
    );
  }
  assert.throws(
    () =>
      applyResearchEvent(selected, {
        ...event,
        research_request_id: OTHER_REQUEST_ID,
      }),
    ResearchSequenceError,
  );
  const unicodeEvent = {
    ...event,
    diagnostic: {
      ...event.diagnostic,
      statements: [
        {
          ...statement,
          evidence: [{ ...range, start_character: 5, end_character: 6 }],
        },
      ],
    },
  };
  assert.equal(applyResearchEvent(selected, unicodeEvent).status, "FAILED");
  assert.equal(Array.from(context.sources[0].text).slice(5, 6).join(""), "😀");
});

test("selected context survives terminal outcomes and clears for the next question", () => {
  const event = selectedContextEvent();
  const selected = applyResearchEvent(beginResearchSession(), event);
  assert.deepEqual(selected.retrievalContext, event.sources);
  assert.equal(selected.status, "RUNNING");
  assert.equal(selected.statements.length, 0);
  assert.deepEqual(
    applyResearchEvent(selected, failedEvent()).retrievalContext,
    event.sources,
  );
  assert.deepEqual(
    applyResearchEvent(selected, {
      event: "answer.insufficient",
      schema_version: "1.0",
      research_request_id: REQUEST_ID,
      coverage: "INSUFFICIENT",
      message: "No supported answer.",
    }).retrievalContext,
    event.sources,
  );
  const completed = [
    statementEvent(),
    citationEvent(),
    completedEvent(),
  ].reduce(applyResearchEvent, selected);
  assert.deepEqual(completed.retrievalContext, event.sources);
  assert.equal(beginResearchSession().retrievalContext, undefined);
});

test("selected context rejects duplicate, foreign-request, and late events", () => {
  const event = selectedContextEvent();
  const selected = applyResearchEvent(beginResearchSession(), event);
  assert.throws(
    () => applyResearchEvent(selected, event),
    ResearchSequenceError,
  );
  const progress = applyResearchEvent(
    beginResearchSession(),
    progressEvent("SELECTING_PASSAGES"),
  );
  assert.throws(
    () => applyResearchEvent(progress, selectedContextEvent(OTHER_REQUEST_ID)),
    ResearchSequenceError,
  );
  for (const stage of [
    "GENERATING_ANSWER",
    "VALIDATING_EVIDENCE",
    "SAVING_RESULT",
  ]) {
    const late = applyResearchEvent(
      beginResearchSession(),
      progressEvent(stage),
    );
    assert.throws(() => applyResearchEvent(late, event), ResearchSequenceError);
  }
  assert.throws(
    () =>
      applyResearchEvent(
        applyResearchEvent(beginResearchSession(), statementEvent()),
        event,
      ),
    ResearchSequenceError,
  );
});

test("a context-limit failure stops progress and preserves the actionable website message", () => {
  const message =
    "Retrieved sources exceed the current research limits. Try a more focused question or select a single document.";
  const session = applyResearchEvent(
    beginResearchSession(),
    progressEvent("SELECTING_PASSAGES"),
  );
  const failure = {
    ...failedEvent(),
    error: {
      code: "RESEARCH_CONTEXT_LIMIT",
      message,
      request_id: REQUEST_ID,
    },
    retriable: false,
  };
  const result = applyResearchEvent(session, failure);

  assert.equal(result.status, "FAILED");
  assert.equal(result.stage, undefined);
  assert.equal(result.message, message);
  assert.deepEqual(result.statements, []);
  assert.throws(
    () => applyResearchEvent(result, progressEvent("GENERATING_ANSWER")),
    ResearchSequenceError,
  );
});

function progressEvent(stage, requestId = REQUEST_ID) {
  return {
    event: "research.progress",
    schema_version: "1.0",
    research_request_id: requestId,
    stage,
  };
}

test("progress tracks only stages received in increasing order", () => {
  const searching = applyResearchEvent(
    beginResearchSession(),
    progressEvent("SEARCHING_SOURCES"),
  );
  const selecting = applyResearchEvent(
    searching,
    progressEvent("SELECTING_PASSAGES"),
  );

  assert.deepEqual(selecting.progressStages, [
    "SEARCHING_SOURCES",
    "SELECTING_PASSAGES",
  ]);
  assert.equal(selecting.stage, "SELECTING_PASSAGES");
  assert.throws(
    () => applyResearchEvent(selecting, progressEvent("SEARCHING_SOURCES")),
    ResearchSequenceError,
  );
  assert.throws(
    () => applyResearchEvent(selecting, progressEvent("SELECTING_PASSAGES")),
    ResearchSequenceError,
  );
});

test("six backend stages become four stable display steps", () => {
  let session = beginResearchSession();
  const stages = [
    "EMBEDDING_QUESTION",
    "SEARCHING_SOURCES",
    "SELECTING_PASSAGES",
    "GENERATING_ANSWER",
    "VALIDATING_EVIDENCE",
    "SAVING_RESULT",
  ];
  const expectedActiveSteps = [
    "PREPARING_QUESTION",
    "FINDING_PASSAGES",
    "FINDING_PASSAGES",
    "DRAFTING_ANSWER",
    "CHECKING_EVIDENCE",
    "CHECKING_EVIDENCE",
  ];

  for (const [index, stage] of stages.entries()) {
    session = applyResearchEvent(session, progressEvent(stage));
    const display = getResearchDisplaySteps(session);
    assert.equal(display.length, 4);
    assert.equal(
      display.find((step) => step.status === "ACTIVE")?.stage,
      expectedActiveSteps[index],
    );
    assert.equal(
      display.filter((step) => step.status === "COMPLETED").length,
      [0, 1, 1, 2, 3, 3][index],
    );
  }
});

test("lexical research does not show a completed embedding step", () => {
  const session = applyResearchEvent(
    beginResearchSession(),
    progressEvent("SEARCHING_SOURCES"),
  );

  assert.deepEqual(getResearchDisplaySteps(session), [
    { stage: "FINDING_PASSAGES", status: "ACTIVE" },
    { stage: "DRAFTING_ANSWER", status: "PENDING" },
    { stage: "CHECKING_EVIDENCE", status: "PENDING" },
  ]);
});

test("a statement becomes complete only after its citation arrives", () => {
  const withStatement = applyResearchEvent(
    beginResearchSession(),
    statementEvent(),
  );
  const withCitation = applyResearchEvent(withStatement, citationEvent());
  const completed = applyResearchEvent(withCitation, completedEvent());

  assert.equal(completed.status, "COMPLETED");
  assert.equal(completed.coverage, "COMPLETE");
  assert.equal(completed.statements[0].text, "Revenue increased by 22.0%.");
  assert.equal(completed.statements[0].citations[0].evidenceSpanId, SPAN_ID);
});

test("partial coverage preserves the validated answer and its citation", () => {
  const completed = applyResearchEvent(
    applyResearchEvent(
      applyResearchEvent(beginResearchSession(), statementEvent()),
      citationEvent(),
    ),
    completedEvent(REQUEST_ID, "PARTIAL"),
  );

  assert.equal(completed.status, "COMPLETED");
  assert.equal(completed.coverage, "PARTIAL");
  assert.equal(completed.statements[0].citations[0].evidenceSpanId, SPAN_ID);
});

test("authorized PDF and text sources attach only to their matching citation", () => {
  const completed = applyResearchEvent(
    applyResearchEvent(
      applyResearchEvent(beginResearchSession(), statementEvent()),
      citationEvent(),
    ),
    completedEvent(),
  );
  const sources = [
    {
      evidence_span_id: SPAN_ID,
      document_id: DOCUMENT_ID,
      document_version_id: VERSION_ID,
      start_character: 10,
      end_character: 24,
      exact_text: "Revenue grew.",
      text_sha256: "a".repeat(64),
      locator: { kind: "PDF_PAGE", page: 2 },
      viewer_url: `/api/v1/documents/${DOCUMENT_ID}/versions/${VERSION_ID}/original#page=2`,
    },
    {
      evidence_span_id: SPAN_ID,
      document_id: DOCUMENT_ID,
      document_version_id: VERSION_ID,
      start_character: 0,
      end_character: 14,
      exact_text: "Revenue grew.",
      text_sha256: "a".repeat(64),
      locator: {
        kind: "TEXT_PASSAGE",
        start_character: 31,
        end_character: 45,
      },
      viewer_url: `/api/v1/documents/${DOCUMENT_ID}/versions/${VERSION_ID}/original#chars=31,45`,
    },
  ];

  for (const source of sources) {
    const resolved = attachCitationSource(completed, source);

    assert.deepEqual(resolved.statements[0].citations[0].source, source);
  }
});

test("an insufficient result remains explicit and contains no statements", () => {
  const result = applyResearchEvent(beginResearchSession(), {
    event: "answer.insufficient",
    schema_version: "1.0",
    research_request_id: REQUEST_ID,
    coverage: "INSUFFICIENT",
    message: "The archive does not contain sufficient evidence.",
  });

  assert.equal(result.status, "INSUFFICIENT");
  assert.equal(result.statements.length, 0);
  assert.match(result.message, /sufficient evidence/);
});

test("a safe provider failure becomes terminal without answer content", () => {
  const result = applyResearchEvent(beginResearchSession(), failedEvent());

  assert.equal(result.status, "FAILED");
  assert.equal(result.researchRequestId, REQUEST_ID);
  assert.equal(
    result.message,
    "The research provider is temporarily unavailable.",
  );
  assert.equal(result.statements.length, 0);
});

test("a failure cannot turn already streamed prose into a partial answer", () => {
  const session = applyResearchEvent(beginResearchSession(), statementEvent());

  assert.throws(
    () => applyResearchEvent(session, failedEvent()),
    ResearchSequenceError,
  );
});

test("a citation cannot arrive before its answer statement", () => {
  assert.throws(
    () => applyResearchEvent(beginResearchSession(), citationEvent()),
    ResearchSequenceError,
  );
});

test("one stream cannot switch to a different research request", () => {
  const session = applyResearchEvent(beginResearchSession(), statementEvent());

  assert.throws(
    () => applyResearchEvent(session, citationEvent(OTHER_REQUEST_ID)),
    ResearchSequenceError,
  );
});

test("completion rejects statements that have no citation", () => {
  const session = applyResearchEvent(beginResearchSession(), statementEvent());

  assert.throws(
    () => applyResearchEvent(session, completedEvent()),
    ResearchSequenceError,
  );
});

test("terminal research sessions reject later events", () => {
  const completed = applyResearchEvent(
    applyResearchEvent(
      applyResearchEvent(beginResearchSession(), statementEvent()),
      citationEvent(),
    ),
    completedEvent(),
  );

  assert.throws(
    () => applyResearchEvent(completed, statementEvent()),
    ResearchSequenceError,
  );
});
