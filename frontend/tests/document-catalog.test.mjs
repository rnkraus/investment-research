import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiProtocolError,
  ApiRequestError,
  createApiClient,
} from "../src/lib/api/client.ts";
import {
  EMPTY_DOCUMENT_SELECTION,
  getResearchDocumentIds,
  resetDocumentSelection,
  toggleDocumentSelection,
} from "../src/features/archive/document-selection.ts";

const DOCUMENT_ID = "10000000-0000-4000-8000-000000000001";
const VERSION_ID = "10000000-0000-4000-8000-000000000002";
const RUN_ID = "10000000-0000-4000-8000-000000000003";
const CREATED_AT = "2026-10-11T09:15:00.123456Z";

function entry(overrides = {}) {
  return {
    document_id: DOCUMENT_ID,
    document_version_id: VERSION_ID,
    title: "Saved synthetic source",
    language: "en",
    media_type: "application/pdf",
    byte_size: 1024,
    created_at: CREATED_AT,
    captured_at: CREATED_AT,
    published_at: null,
    available_at: null,
    processing: {
      processing_run_id: RUN_ID,
      document_version_id: VERSION_ID,
      state: "SEARCHABLE",
      error_summary: null,
    },
    original_url: `/api/v1/documents/${DOCUMENT_ID}/versions/${VERSION_ID}/original`,
    ...overrides,
  };
}

function clientWithPage(page) {
  return createApiClient(
    async () =>
      new Response(JSON.stringify(page), {
        headers: { "content-type": "application/json" },
      }),
  );
}

test("catalog requests bounded pages with both exact cursor fields, no cache, and cancellation", async () => {
  const cursor = {
    created_at: "2026-10-11T10:00:00.123456+01:00",
    document_id: RUN_ID,
  };
  const controller = new AbortController();
  let receivedPath;
  let receivedInit;
  const page = {
    items: [entry()],
    next_cursor: { created_at: CREATED_AT, document_id: DOCUMENT_ID },
  };
  const client = createApiClient(async (path, init) => {
    receivedPath = path;
    receivedInit = init;
    return new Response(JSON.stringify(page));
  });
  assert.deepEqual(await client.listDocuments(cursor, controller.signal), page);
  const query = new URL(receivedPath, "http://local.invalid");
  assert.equal(query.pathname, "/api/v1/documents");
  assert.deepEqual(Object.fromEntries(query.searchParams), {
    page_size: "20",
    after_created_at: cursor.created_at,
    after_document_id: cursor.document_id,
  });
  assert.equal(receivedInit.cache, "no-store");
  assert.equal(receivedInit.signal, controller.signal);
});

test("catalog accepts empty, text, pending, missing, and failed processing snapshots", async () => {
  assert.deepEqual(
    await clientWithPage({ items: [], next_cursor: null }).listDocuments(),
    { items: [], next_cursor: null },
  );
  for (const state of [
    null,
    "CAPTURED",
    "PARSING",
    "LEXICAL_READY",
    "INDEXING",
    "SEARCHABLE",
    "FAILED",
  ]) {
    const document = entry({
      media_type: "text/plain",
      processing:
        state === null
          ? null
          : {
              processing_run_id: RUN_ID,
              document_version_id: VERSION_ID,
              state,
              error_summary:
                state === "FAILED" ? "Processing stopped safely." : null,
            },
    });
    assert.deepEqual(
      (await clientWithPage({ items: [document] }).listDocuments()).items,
      [document],
    );
  }
});

test("catalog title limits count Unicode code points as the backend does", async () => {
  const title = "🧪".repeat(300);
  assert.equal(
    (await clientWithPage({ items: [entry({ title })] }).listDocuments())
      .items[0].title,
    title,
  );
  await assert.rejects(
    clientWithPage({ items: [entry({ title: `${title}x` })] }).listDocuments(),
    ApiProtocolError,
  );
});

test("catalog rejects invalid metadata and mismatched status or original routes", async () => {
  for (const overrides of [
    { document_id: "not-a-uuid" },
    { document_version_id: "invalid" },
    { title: "" },
    { language: "" },
    { byte_size: 0 },
    { byte_size: 1.5 },
    { media_type: "text/html" },
    { created_at: "yesterday" },
    { captured_at: "2026-10-11T09:15:00" },
    { published_at: "not-a-date" },
    { available_at: "not-a-date" },
    { processing: undefined },
    {
      processing: {
        processing_run_id: RUN_ID,
        document_version_id: RUN_ID,
        state: "SEARCHABLE",
      },
    },
    {
      processing: {
        processing_run_id: RUN_ID,
        document_version_id: VERSION_ID,
        state: "UNKNOWN",
      },
    },
    { original_url: "https://example.com/original" },
    {
      original_url: `/api/v1/documents/${DOCUMENT_ID}/versions/${RUN_ID}/original`,
    },
    { original_url: `${entry().original_url}?token=private` },
  ]) {
    await assert.rejects(
      clientWithPage({ items: [entry(overrides)] }).listDocuments(),
      ApiProtocolError,
    );
  }
});

test("catalog rejects duplicate documents, oversized pages, and invalid continuation cursors", async () => {
  for (const page of [
    { items: [entry(), entry()] },
    {
      items: Array.from({ length: 21 }, (_, index) =>
        entry({
          document_id: `10000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
          original_url: `/api/v1/documents/10000000-0000-4000-8000-${String(index).padStart(12, "0")}/versions/${VERSION_ID}/original`,
        }),
      ),
    },
    {
      items: [],
      next_cursor: { created_at: CREATED_AT, document_id: DOCUMENT_ID },
    },
    {
      items: [entry()],
      next_cursor: { created_at: CREATED_AT, document_id: RUN_ID },
    },
    {
      items: [entry()],
      next_cursor: { created_at: "invalid", document_id: DOCUMENT_ID },
    },
    { items: [entry()], next_cursor: "invalid" },
  ]) {
    await assert.rejects(
      clientWithPage(page).listDocuments(),
      ApiProtocolError,
    );
  }
});

test("catalog rejects a non-advancing cursor and invalid requests without fetching", async () => {
  const cursor = { created_at: CREATED_AT, document_id: DOCUMENT_ID };
  await assert.rejects(
    clientWithPage({ items: [entry()], next_cursor: cursor }).listDocuments(
      cursor,
    ),
    ApiProtocolError,
  );
  let fetches = 0;
  const client = createApiClient(async () => {
    fetches += 1;
    throw new Error("must not fetch");
  });
  await assert.rejects(
    client.listDocuments({ created_at: "invalid", document_id: DOCUMENT_ID }),
    ApiProtocolError,
  );
  assert.equal(fetches, 0);
});

test("catalog failure never exposes an unstructured server response", async () => {
  const client = createApiClient(
    async () => new Response("private detail", { status: 500 }),
  );
  await assert.rejects(client.listDocuments(), (error) => {
    assert.ok(error instanceof ApiRequestError);
    assert.doesNotMatch(error.message, /private detail/);
    return true;
  });
});

test("all-source scope is explicit; empty focused scope never expands to all sources", () => {
  assert.deepEqual(getResearchDocumentIds(EMPTY_DOCUMENT_SELECTION), []);
  assert.equal(
    getResearchDocumentIds({ scope: "SELECTED", documents: [] }),
    undefined,
  );
  const selected = toggleDocumentSelection(EMPTY_DOCUMENT_SELECTION, entry());
  assert.deepEqual(getResearchDocumentIds(selected), [DOCUMENT_ID]);
  assert.equal(
    getResearchDocumentIds(toggleDocumentSelection(selected, entry())),
    undefined,
  );
  assert.deepEqual(resetDocumentSelection(selected), {
    scope: "SELECTED",
    documents: [],
  });
});

test("multiple selected sources preserve their exact IDs independently of the visible page", () => {
  const first = toggleDocumentSelection(EMPTY_DOCUMENT_SELECTION, entry());
  const second = toggleDocumentSelection(first, entry({ document_id: RUN_ID }));
  assert.deepEqual(getResearchDocumentIds(second), [DOCUMENT_ID, RUN_ID]);
  assert.deepEqual(
    getResearchDocumentIds(toggleDocumentSelection(second, entry())),
    [RUN_ID],
  );
  assert.equal(
    getResearchDocumentIds({
      scope: "SELECTED",
      documents: [entry(), entry()],
    }),
    undefined,
  );
});

test("clearing several selected documents keeps focused scope and does not mutate the previous selection", () => {
  const thirdId = "20000000-0000-4000-8000-000000000003";
  const selected = [DOCUMENT_ID, RUN_ID, thirdId].reduce(
    (current, document_id) =>
      toggleDocumentSelection(current, entry({ document_id })),
    EMPTY_DOCUMENT_SELECTION,
  );
  assert.deepEqual(getResearchDocumentIds(selected), [
    DOCUMENT_ID,
    RUN_ID,
    thirdId,
  ]);
  const cleared = resetDocumentSelection(selected);
  assert.deepEqual(cleared, { scope: "SELECTED", documents: [] });
  assert.equal(getResearchDocumentIds(cleared), undefined);
  assert.equal(selected.documents.length, 3);
  assert.deepEqual(
    resetDocumentSelection(EMPTY_DOCUMENT_SELECTION),
    EMPTY_DOCUMENT_SELECTION,
  );
});

test("missing, pending, and failed processing cannot be selected", () => {
  for (const state of [
    null,
    "CAPTURED",
    "PARSING",
    "LEXICAL_READY",
    "INDEXING",
    "FAILED",
  ]) {
    const document = entry({
      processing: state === null ? null : { ...entry().processing, state },
    });
    assert.equal(
      toggleDocumentSelection(EMPTY_DOCUMENT_SELECTION, document),
      EMPTY_DOCUMENT_SELECTION,
    );
    assert.equal(
      getResearchDocumentIds({ scope: "SELECTED", documents: [document] }),
      undefined,
    );
  }
});

test("selection is bounded at 100, but removing an existing source remains possible", () => {
  const documents = Array.from({ length: 100 }, (_, index) =>
    entry({
      document_id: `20000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    }),
  );
  const selection = { scope: "SELECTED", documents };
  assert.equal(getResearchDocumentIds(selection).length, 100);
  assert.equal(toggleDocumentSelection(selection, entry()), selection);
  assert.equal(
    toggleDocumentSelection(selection, documents[0]).documents.length,
    99,
  );
  assert.equal(
    getResearchDocumentIds({
      scope: "SELECTED",
      documents: [...documents, entry()],
    }),
    undefined,
  );
});
