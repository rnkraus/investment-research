import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiProtocolError,
  ApiRequestError,
  createApiClient,
  requireLocalApiPath,
} from "../src/lib/api/client.ts";

const DOCUMENT_ID = "10000000-0000-4000-8000-000000000001";
const VERSION_ID = "10000000-0000-4000-8000-000000000002";
const PROCESSING_RUN_ID = "10000000-0000-4000-8000-000000000003";
const RESEARCH_REQUEST_ID = "10000000-0000-4000-8000-000000000004";
const STATEMENT_ID = "10000000-0000-4000-8000-000000000005";
const EVIDENCE_SPAN_ID = "10000000-0000-7000-8000-000000000006";

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

test("captureDocument sends the exact multipart API fields", async () => {
  let capturedPath;
  let capturedInit;
  const client = createApiClient(async (path, init) => {
    capturedPath = path;
    capturedInit = init;
    return jsonResponse(
      {
        document_id: DOCUMENT_ID,
        document_version_id: VERSION_ID,
        processing_run_id: PROCESSING_RUN_ID,
        processing_status_url: `/api/v1/processing-runs/${PROCESSING_RUN_ID}`,
        state: "CAPTURED",
      },
      202,
    );
  });
  const file = new File(["%PDF-1.7\n"], "earnings.pdf", {
    type: "application/pdf",
  });

  const result = await client.captureDocument({
    file,
    title: "FY2025 earnings",
    language: "en",
    publishedAt: "2026-02-20T12:00:00Z",
  });

  assert.equal(capturedPath, "/api/v1/documents");
  assert.equal(capturedInit.method, "POST");
  assert.ok(capturedInit.body instanceof FormData);
  assert.equal(capturedInit.body.get("file").name, "earnings.pdf");
  assert.equal(capturedInit.body.get("title"), "FY2025 earnings");
  assert.equal(capturedInit.body.get("language"), "en");
  assert.equal(capturedInit.body.get("published_at"), "2026-02-20T12:00:00Z");
  assert.equal(capturedInit.body.get("available_at"), null);
  assert.equal(result.processing_run_id, PROCESSING_RUN_ID);
});

test("API errors preserve only the safe public error contract", async () => {
  const client = createApiClient(async () =>
    jsonResponse(
      {
        error: {
          code: "NOT_FOUND",
          message: "evidence span was not found",
          request_id: RESEARCH_REQUEST_ID,
        },
      },
      404,
    ),
  );

  await assert.rejects(
    client.resolveEvidenceSpan(EVIDENCE_SPAN_ID),
    (error) => {
      assert.ok(error instanceof ApiRequestError);
      assert.equal(error.status, 404);
      assert.equal(error.code, "NOT_FOUND");
      assert.equal(error.message, "evidence span was not found");
      assert.equal(error.requestId, RESEARCH_REQUEST_ID);
      return true;
    },
  );
});

test("successful JSON responses still require the runtime contract", async () => {
  const client = createApiClient(async () =>
    jsonResponse({
      processing_run_id: PROCESSING_RUN_ID,
      state: "LEXICAL_READY",
    }),
  );

  await assert.rejects(
    client.getProcessingRun(`/api/v1/processing-runs/${PROCESSING_RUN_ID}`),
    ApiProtocolError,
  );
});

test("streamResearch decodes validated events split across network chunks", async () => {
  const frames = [
    {
      event: "answer.statement",
      schema_version: "1.0",
      research_request_id: RESEARCH_REQUEST_ID,
      statement_id: STATEMENT_ID,
      order: 0,
      text: "Revenue increased.",
    },
    {
      event: "answer.citation",
      schema_version: "1.0",
      research_request_id: RESEARCH_REQUEST_ID,
      statement_id: STATEMENT_ID,
      evidence_span_id: EVIDENCE_SPAN_ID,
      relationship: "SUPPORTS",
      display_order: 0,
    },
    {
      event: "answer.completed",
      schema_version: "1.0",
      research_request_id: RESEARCH_REQUEST_ID,
      coverage: "COMPLETE",
    },
  ].map((event) => `event: ${event.event}\ndata: ${JSON.stringify(event)}\n\n`);
  const wirePayload = frames.join("").replaceAll("\n", "\r\n");
  const encoded = new TextEncoder().encode(wirePayload);
  const splitAt = wirePayload.indexOf("\r\n") + 1;
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(encoded.slice(0, splitAt));
      controller.enqueue(encoded.slice(splitAt));
      controller.close();
    },
  });
  const client = createApiClient(
    async () =>
      new Response(body, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
  );

  const events = [];
  for await (const event of client.streamResearch({
    question: "What changed?",
  })) {
    events.push(event);
  }

  assert.deepEqual(
    events.map((event) => event.event),
    ["answer.statement", "answer.citation", "answer.completed"],
  );
  assert.equal(events[1].evidence_span_id, EVIDENCE_SPAN_ID);
});

test("local API paths allow protected viewer fragments and reject external targets", () => {
  const viewerPath = `/api/v1/documents/${DOCUMENT_ID}/versions/${VERSION_ID}/original#page=2`;

  assert.equal(requireLocalApiPath(viewerPath), viewerPath);
  assert.throws(
    () => requireLocalApiPath("https://example.com/api/v1/documents"),
    ApiProtocolError,
  );
  assert.throws(
    () => requireLocalApiPath("//example.com/api/v1/documents"),
    ApiProtocolError,
  );
  assert.throws(
    () => requireLocalApiPath("/api/v1\\documents"),
    ApiProtocolError,
  );
  assert.throws(() => requireLocalApiPath("/not-the-api"), ApiProtocolError);
});
