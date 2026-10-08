import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiProtocolError,
  ApiRequestError,
  ApiTransportError,
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

async function collectResearch(client) {
  const events = [];
  for await (const event of client.streamResearch({
    question: "What changed?",
  })) {
    events.push(event);
  }
  return events;
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

test("research model options accept only the active runtime's allowlisted models", async () => {
  const options = {
    provider_enabled: true,
    generation_models: ["gpt-5.6-terra", "gpt-5.6-luna"],
    support_models: ["gpt-5.6-terra", "gpt-5.6-luna"],
  };
  let requestedPath;
  const client = createApiClient(async (path) => {
    requestedPath = path;
    return jsonResponse(options);
  });

  assert.deepEqual(await client.getResearchModelOptions(), options);
  assert.equal(requestedPath, "/api/v1/research-requests/models");

  const deterministicClient = createApiClient(async () =>
    jsonResponse({
      provider_enabled: false,
      generation_models: [],
      support_models: [],
    }),
  );
  assert.equal(
    (await deterministicClient.getResearchModelOptions()).provider_enabled,
    false,
  );
});

test("research model options reject unapproved or inconsistent values", async () => {
  for (const options of [
    {
      provider_enabled: true,
      generation_models: ["unknown-model"],
      support_models: ["gpt-5.6-terra"],
    },
    {
      provider_enabled: true,
      generation_models: ["gpt-5.6-terra"],
      support_models: [],
    },
    {
      provider_enabled: false,
      generation_models: ["gpt-5.6-terra"],
      support_models: [],
    },
    {
      provider_enabled: true,
      generation_models: ["gpt-5.6-terra", "gpt-5.6-terra"],
      support_models: ["gpt-5.6-terra"],
    },
  ]) {
    const client = createApiClient(async () => jsonResponse(options));
    await assert.rejects(client.getResearchModelOptions(), ApiProtocolError);
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
      event: "research.progress",
      schema_version: "1.0",
      research_request_id: RESEARCH_REQUEST_ID,
      stage: "EMBEDDING_QUESTION",
    },
    {
      event: "research.progress",
      schema_version: "1.0",
      research_request_id: RESEARCH_REQUEST_ID,
      stage: "SEARCHING_SOURCES",
    },
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
  const wirePayload = `: keep-alive\n\n${frames.join("")}`.replaceAll(
    "\n",
    "\r\n",
  );
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
    [
      "research.progress",
      "research.progress",
      "answer.statement",
      "answer.citation",
      "answer.completed",
    ],
  );
  assert.equal(events[3].evidence_span_id, EVIDENCE_SPAN_ID);
});

test("streamResearch sends independently selected answer and support models", async () => {
  let requestPath;
  let requestBody;
  const client = createApiClient(async (path, init) => {
    requestPath = path;
    requestBody = JSON.parse(init.body);
    return new Response("", {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  });

  for await (const event of client.streamResearch({
    question: "What changed?",
    generation_model: "gpt-5.6-luna",
    support_model: "gpt-5.6-terra",
    context: { document_ids: [DOCUMENT_ID], temporal_mode: "CURRENT" },
  })) {
    assert.fail(`the empty test stream emitted ${event.event}`);
  }

  assert.equal(requestPath, "/api/v1/research-requests");
  assert.deepEqual(requestBody, {
    question: "What changed?",
    generation_model: "gpt-5.6-luna",
    support_model: "gpt-5.6-terra",
    context: { document_ids: [DOCUMENT_ID], temporal_mode: "CURRENT" },
  });
});

test(
  "streamResearch yields progress before the response stream closes",
  { timeout: 2_000 },
  async () => {
    const progress = {
      event: "research.progress",
      schema_version: "1.0",
      research_request_id: RESEARCH_REQUEST_ID,
      stage: "EMBEDDING_QUESTION",
    };
    let closeStream;
    const streamClosed = new Promise((resolve) => {
      closeStream = resolve;
    });
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            `event: research.progress\ndata: ${JSON.stringify(progress)}\n\n`,
          ),
        );
        void streamClosed.then(() => controller.close());
      },
    });
    const client = createApiClient(
      async () =>
        new Response(body, {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
    );
    const events = client.streamResearch({ question: "What changed?" });

    try {
      const first = await events.next();
      assert.equal(first.done, false);
      assert.deepEqual(first.value, progress);
    } finally {
      closeStream();
      await events.return();
    }
  },
);

test("streamResearch rejects an unknown progress stage", async () => {
  const frame = {
    event: "research.progress",
    schema_version: "1.0",
    research_request_id: RESEARCH_REQUEST_ID,
    stage: "UNTRUSTED_STAGE",
  };
  const client = createApiClient(
    async () =>
      new Response(
        `event: research.progress\ndata: ${JSON.stringify(frame)}\n\n`,
        {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        },
      ),
  );

  await assert.rejects(collectResearch(client), ApiProtocolError);
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

test("streamResearch accepts a bounded context-limit failure without treating it as a provider outage", async () => {
  const failure = {
    event: "research.failed",
    schema_version: "1.0",
    research_request_id: RESEARCH_REQUEST_ID,
    error: {
      code: "RESEARCH_CONTEXT_LIMIT",
      message:
        "Retrieved sources exceed the current research limits. Try a more focused question or select a single document.",
      request_id: RESEARCH_REQUEST_ID,
    },
    retriable: false,
  };
  const client = createApiClient(
    async () =>
      new Response(
        `event: research.failed\ndata: ${JSON.stringify(failure)}\n\n`,
        {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        },
      ),
  );

  assert.deepEqual(await collectResearch(client), [failure]);
});

test("unstructured research proxy failures explain the HTTP error without exposing the body", async () => {
  const client = createApiClient(
    async () =>
      new Response("private provider detail", {
        status: 500,
        headers: { "x-request-id": RESEARCH_REQUEST_ID },
      }),
  );

  await assert.rejects(collectResearch(client), (error) => {
    assert.ok(error instanceof ApiRequestError);
    assert.equal(error.status, 500);
    assert.equal(error.requestId, RESEARCH_REQUEST_ID);
    assert.match(error.message, /HTTP 500/);
    assert.match(error.message, /may have timed out/);
    assert.match(error.message, /another provider charge/);
    assert.doesNotMatch(error.message, /private provider detail/);
    return true;
  });
});

test("unknown research error codes never expose an untrusted response message", async () => {
  const client = createApiClient(async () =>
    jsonResponse(
      { error: { code: "UNKNOWN", message: "private provider detail" } },
      502,
    ),
  );

  await assert.rejects(collectResearch(client), (error) => {
    assert.ok(error instanceof ApiRequestError);
    assert.equal(error.code, undefined);
    assert.match(error.message, /HTTP 502/);
    assert.doesNotMatch(error.message, /private provider detail/);
    return true;
  });
});

test("research connection failures have a safe explanation", async () => {
  const client = createApiClient(async () => {
    throw new Error("private network detail");
  });

  await assert.rejects(collectResearch(client), (error) => {
    assert.ok(error instanceof ApiTransportError);
    assert.match(error.message, /connection to the research API failed/);
    assert.doesNotMatch(error.message, /private network detail/);
    return true;
  });
});

test("interrupted research streams have a safe explanation", async () => {
  const client = createApiClient(
    async () =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.error(new Error("private stream detail"));
          },
        }),
        { status: 200, headers: { "content-type": "text/event-stream" } },
      ),
  );

  await assert.rejects(collectResearch(client), (error) => {
    assert.ok(error instanceof ApiTransportError);
    assert.match(error.message, /closed before the answer finished/);
    assert.doesNotMatch(error.message, /private stream detail/);
    return true;
  });
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
