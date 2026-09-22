import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiProtocolError,
  ApiRequestError,
  createApiClient,
  requireLocalApiPath,
  requireLocalSourcePath,
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

test("capturePastedText sends the exact JSON API contract", async () => {
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

  const result = await client.capturePastedText({
    title: "Research note",
    language: "en",
    published_at: "2026-02-20T12:00:00Z",
    text: "Revenue increased.",
  });

  assert.equal(capturedPath, "/api/v1/documents/text");
  assert.equal(capturedInit.method, "POST");
  assert.equal(capturedInit.headers["content-type"], "application/json");
  assert.deepEqual(JSON.parse(capturedInit.body), {
    title: "Research note",
    language: "en",
    published_at: "2026-02-20T12:00:00Z",
    text: "Revenue increased.",
  });
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

test("processing status accepts the complete M1 indexing lifecycle", async () => {
  for (const state of ["INDEXING", "SEARCHABLE"]) {
    const client = createApiClient(async () =>
      jsonResponse({
        processing_run_id: PROCESSING_RUN_ID,
        document_version_id: VERSION_ID,
        state,
        error_summary: null,
      }),
    );

    const result = await client.getProcessingRun(
      `/api/v1/processing-runs/${PROCESSING_RUN_ID}`,
    );

    assert.equal(result.state, state);
  }
});

test("evidence spans accept PDF-page and text-passage locators", async () => {
  const locators = [
    { kind: "PDF_PAGE", page: 2 },
    { kind: "TEXT_PASSAGE", start_character: 11, end_character: 29 },
  ];

  for (const locator of locators) {
    const client = createApiClient(async () =>
      jsonResponse({
        evidence_span_id: EVIDENCE_SPAN_ID,
        document_id: DOCUMENT_ID,
        document_version_id: VERSION_ID,
        start_character: 0,
        end_character: 18,
        exact_text: "Revenue increased.",
        text_sha256: "a".repeat(64),
        locator,
        viewer_url: `/api/v1/documents/${DOCUMENT_ID}/versions/${VERSION_ID}/original`,
      }),
    );

    const result = await client.resolveEvidenceSpan(EVIDENCE_SPAN_ID);

    assert.deepEqual(result.locator, locator);
  }
});

test("evidence spans reject unknown source locator kinds", async () => {
  const client = createApiClient(async () =>
    jsonResponse({
      evidence_span_id: EVIDENCE_SPAN_ID,
      document_id: DOCUMENT_ID,
      document_version_id: VERSION_ID,
      start_character: 0,
      end_character: 18,
      exact_text: "Revenue increased.",
      text_sha256: "a".repeat(64),
      locator: { kind: "WEB_URL", url: "https://example.com" },
      viewer_url: `/api/v1/documents/${DOCUMENT_ID}/versions/${VERSION_ID}/original`,
    }),
  );

  await assert.rejects(
    client.resolveEvidenceSpan(EVIDENCE_SPAN_ID),
    ApiProtocolError,
  );
});

test("openOriginal loads only the two supported immutable source types", async () => {
  const requestedPaths = [];
  const client = createApiClient(async (path) => {
    requestedPaths.push(path);
    if (requestedPaths.length === 1) {
      return new Response(
        new Blob(["%PDF-1.7\n"], { type: "application/pdf" }),
        {
          headers: { "content-type": "application/pdf" },
        },
      );
    }
    return new Response("Revenue increased.\n", {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  });

  const pdf = await client.openOriginal(DOCUMENT_ID, VERSION_ID);
  const text = await client.openOriginal(DOCUMENT_ID, VERSION_ID);

  assert.equal(pdf.kind, "PDF");
  assert.equal(pdf.content.size, 9);
  assert.deepEqual(text, { kind: "TEXT", content: "Revenue increased.\n" });
  assert.deepEqual(requestedPaths, [
    `/api/v1/documents/${DOCUMENT_ID}/versions/${VERSION_ID}/original`,
    `/api/v1/documents/${DOCUMENT_ID}/versions/${VERSION_ID}/original`,
  ]);
});

test("openOriginal rejects unsafe identifiers and unsupported content", async () => {
  let fetchCalls = 0;
  const invalidIdentifierClient = createApiClient(async () => {
    fetchCalls += 1;
    return new Response();
  });

  await assert.rejects(
    invalidIdentifierClient.openOriginal("../documents", VERSION_ID),
    ApiProtocolError,
  );
  assert.equal(fetchCalls, 0);

  const emptyPdfClient = createApiClient(
    async () =>
      new Response(new Blob([], { type: "application/pdf" }), {
        headers: { "content-type": "application/pdf" },
      }),
  );
  await assert.rejects(
    emptyPdfClient.openOriginal(DOCUMENT_ID, VERSION_ID),
    ApiProtocolError,
  );

  const htmlClient = createApiClient(
    async () =>
      new Response("<p>not an original</p>", {
        headers: { "content-type": "text/html" },
      }),
  );
  await assert.rejects(
    htmlClient.openOriginal(DOCUMENT_ID, VERSION_ID),
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

test("streamResearch accepts the safe provider-unavailable terminal event", async () => {
  const failure = {
    event: "research.failed",
    schema_version: "1.0",
    research_request_id: RESEARCH_REQUEST_ID,
    error: {
      code: "PROVIDER_UNAVAILABLE",
      message: "research provider is unavailable",
      request_id: RESEARCH_REQUEST_ID,
    },
    retriable: true,
  };
  const body = `event: research.failed\ndata: ${JSON.stringify(failure)}\n\n`;
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

  assert.deepEqual(events, [failure]);
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

test("local source paths require exactly one opaque evidence identifier", () => {
  const sourcePath = `/sources/${EVIDENCE_SPAN_ID}`;

  assert.equal(requireLocalSourcePath(sourcePath), sourcePath);
  for (const unsafePath of [
    `https://example.com${sourcePath}`,
    `//example.com${sourcePath}`,
    `${sourcePath}?download=true`,
    `${sourcePath}#page=2`,
    "/sources/not-a-uuid",
    `${sourcePath}/../${STATEMENT_ID}`,
    `/sources\\${EVIDENCE_SPAN_ID}`,
  ]) {
    assert.throws(() => requireLocalSourcePath(unsafePath), ApiProtocolError);
  }
});
