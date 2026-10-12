import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiProtocolError,
  ApiRequestError,
  createApiClient,
} from "../src/lib/api/client.ts";
import {
  acceptDeletionBatch,
  beginDeletionBatch,
  isDeletionComplete,
  refreshDeletionBatch,
  waitForDeletionCheck,
} from "../src/features/archive/document-deletion.ts";

const FIRST = "10000000-0000-4000-8000-000000000001";
const SECOND = "10000000-0000-4000-8000-000000000002";
const REQUESTED = "2026-10-12T12:00:00Z";
const COMPLETED = "2026-10-12T12:01:00Z";
const input = { document_ids: [FIRST, SECOND], confirm_permanent: true };

function receipt(id = FIRST, state = "PENDING") {
  return {
    document_id: id,
    state,
    requested_at: REQUESTED,
    completed_at: state === "COMPLETED" ? COMPLETED : null,
    status_url: `/api/v1/documents/${id}/deletion`,
  };
}

function accepted(overrides = {}) {
  return {
    items: [receipt(FIRST), receipt(SECOND)],
    dispatch_pending_document_ids: [],
    ...overrides,
  };
}

function respond(payload, status = 202) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

test("deletion sends only the explicit confirmed batch, without cache and with cancellation", async () => {
  const controller = new AbortController();
  let calls = 0;
  const client = createApiClient(async (path, init) => {
    calls += 1;
    assert.equal(path, "/api/v1/documents/deletions");
    assert.equal(init.method, "POST");
    assert.equal(init.cache, "no-store");
    assert.equal(init.headers["content-type"], "application/json");
    assert.equal(init.signal, controller.signal);
    assert.deepEqual(JSON.parse(init.body), input);
    return respond(accepted({ dispatch_pending_document_ids: [SECOND] }));
  });
  const result = await client.deleteDocuments(input, controller.signal);
  assert.deepEqual(result.dispatch_pending_document_ids, [SECOND]);
  assert.equal(calls, 1);
});

test("invalid or implicit deletion never reaches the network", async () => {
  const client = createApiClient(async () => {
    assert.fail("must not fetch");
  });
  for (const invalid of [
    null,
    {},
    { ...input, confirm_permanent: false },
    { ...input, confirm_permanent: 1 },
    { ...input, confirm_permanent: "true" },
    { document_ids: [FIRST] },
    { ...input, workspace_id: FIRST },
    { ...input, document_ids: [] },
    { ...input, document_ids: [FIRST, FIRST] },
    { ...input, document_ids: ["invalid"] },
    {
      ...input,
      document_ids: Array.from(
        { length: 101 },
        (_, i) => `10000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
      ),
    },
  ]) {
    await assert.rejects(client.deleteDocuments(invalid), ApiProtocolError);
  }
});

test("deletion rejects foreign, duplicate, reordered, or inconsistent receipt batches", async () => {
  for (const payload of [
    accepted({ items: [] }),
    accepted({ items: [receipt(FIRST)] }),
    accepted({ items: [receipt(SECOND), receipt(FIRST)] }),
    accepted({ items: [receipt(FIRST), receipt(FIRST)] }),
    accepted({
      dispatch_pending_document_ids: ["10000000-0000-4000-8000-000000000099"],
    }),
    accepted({ dispatch_pending_document_ids: [FIRST, FIRST] }),
    accepted({
      items: [receipt(FIRST, "COMPLETED"), receipt(SECOND)],
      dispatch_pending_document_ids: [FIRST],
    }),
    accepted({ internal_target: "not a public field" }),
  ]) {
    const client = createApiClient(async () => respond(payload));
    await assert.rejects(client.deleteDocuments(input), ApiProtocolError);
  }
});

test("receipt reads require the exact scoped ID and a coherent content-free shape", async () => {
  const controller = new AbortController();
  const client = createApiClient(async (path, init) => {
    assert.equal(path, `/api/v1/documents/${FIRST}/deletion`);
    assert.equal(init.cache, "no-store");
    assert.equal(init.signal, controller.signal);
    return respond(receipt(FIRST, "COMPLETED"), 200);
  });
  assert.equal(
    (await client.getDocumentDeletion(FIRST, controller.signal)).state,
    "COMPLETED",
  );
  for (const payload of [
    receipt(SECOND),
    { ...receipt(), state: "UNKNOWN" },
    { ...receipt(), completed_at: COMPLETED },
    { ...receipt(FIRST, "COMPLETED"), completed_at: null },
    { ...receipt(FIRST, "COMPLETED"), completed_at: "2026-10-11T12:00:00Z" },
    { ...receipt(), requested_at: "yesterday" },
    { ...receipt(), requested_at: "2026-10-12T12:00:00" },
    { ...receipt(), status_url: "https://untrusted.invalid/deletion" },
    { ...receipt(), status_url: `/api/v1/documents/${SECOND}/deletion` },
    { ...receipt(), title: "unexpected content" },
  ]) {
    await assert.rejects(
      createApiClient(async () => respond(payload, 200)).getDocumentDeletion(
        FIRST,
      ),
      ApiProtocolError,
    );
  }
  await assert.rejects(client.getDocumentDeletion("invalid"), ApiProtocolError);
});

test("deletion failures never expose an unstructured response body", async () => {
  for (const status of [404, 409, 422, 500]) {
    const client = createApiClient(
      async () => new Response("private backend details", { status }),
    );
    await assert.rejects(client.deleteDocuments(input), (error) => {
      assert.ok(error instanceof ApiRequestError);
      assert.equal(error.status, status);
      assert.doesNotMatch(error.message, /private backend/);
      return true;
    });
  }
});

test("a confirmation snapshot does not change when visible selection changes", () => {
  const ids = [FIRST, SECOND];
  const batch = beginDeletionBatch(ids);
  ids.pop();
  assert.deepEqual(batch.documentIds, [FIRST, SECOND]);
  assert.equal(isDeletionComplete(batch), false);
  assert.equal(isDeletionComplete(beginDeletionBatch([])), false);
});

test("202 and successful queue publication do not imply cleanup completion", () => {
  const batch = acceptDeletionBatch(
    beginDeletionBatch(input.document_ids),
    accepted(),
  );
  assert.equal(batch.unavailableDocumentIds.length, 0);
  assert.equal(batch.dispatchPendingDocumentIds.length, 0);
  assert.equal(isDeletionComplete(batch), false);
});

test("partial completion retains pending targets and never rereads completed receipts", async () => {
  const batch = acceptDeletionBatch(
    beginDeletionBatch(input.document_ids),
    accepted({
      items: [receipt(FIRST, "COMPLETED"), receipt(SECOND)],
      dispatch_pending_document_ids: [SECOND],
    }),
  );
  let calls = 0;
  const result = await refreshDeletionBatch(
    batch,
    {
      async getDocumentDeletion(id) {
        calls += 1;
        assert.equal(id, SECOND);
        return receipt(id, "COMPLETED");
      },
    },
    new AbortController().signal,
  );
  assert.equal(calls, 1);
  assert.equal(isDeletionComplete(result), true);
  assert.deepEqual(result.dispatchPendingDocumentIds, []);
  assert.equal(isDeletionComplete(batch), false);
});

test("404, network errors, and malformed status responses leave completion unconfirmed", async () => {
  for (const error of [
    new ApiRequestError("Not found", { status: 404 }),
    new TypeError("offline"),
    new ApiProtocolError(),
  ]) {
    const result = await refreshDeletionBatch(
      beginDeletionBatch(input.document_ids),
      {
        async getDocumentDeletion(id) {
          if (id === FIRST) return receipt(id, "COMPLETED");
          throw error;
        },
      },
      new AbortController().signal,
    );
    assert.equal(isDeletionComplete(result), false);
    assert.deepEqual(result.unavailableDocumentIds, [SECOND]);
    assert.deepEqual(result.receipts, [receipt(FIRST, "COMPLETED")]);
  }
});

test("a failed status refresh keeps the last confirmed pending receipt", async () => {
  const batch = acceptDeletionBatch(
    beginDeletionBatch([FIRST]),
    accepted({ items: [receipt()] }),
  );
  const result = await refreshDeletionBatch(
    batch,
    {
      async getDocumentDeletion() {
        throw new TypeError("offline");
      },
    },
    new AbortController().signal,
  );
  assert.deepEqual(result.receipts, batch.receipts);
  assert.deepEqual(result.unavailableDocumentIds, [FIRST]);
  assert.equal(isDeletionComplete(result), false);
});

test("status reading bounds concurrency at five and only queries confirmed targets", async () => {
  const ids = Array.from(
    { length: 12 },
    (_, i) => `10000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
  );
  let active = 0;
  let peak = 0;
  const seen = [];
  const result = await refreshDeletionBatch(
    beginDeletionBatch(ids),
    {
      async getDocumentDeletion(id, signal) {
        assert.equal(signal.aborted, false);
        active += 1;
        peak = Math.max(peak, active);
        seen.push(id);
        await new Promise((resolve) => setImmediate(resolve));
        active -= 1;
        return receipt(id, "COMPLETED");
      },
    },
    new AbortController().signal,
  );
  assert.equal(peak, 5);
  assert.deepEqual(seen, ids);
  assert.equal(isDeletionComplete(result), true);
});

test("cancellation stops status reads and the scheduled next check", async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    refreshDeletionBatch(
      beginDeletionBatch([FIRST]),
      {
        async getDocumentDeletion() {
          assert.fail("must not fetch");
        },
      },
      controller.signal,
    ),
    { name: "AbortError" },
  );
  await assert.rejects(waitForDeletionCheck(controller.signal), {
    name: "AbortError",
  });
  const next = new AbortController();
  const waiting = waitForDeletionCheck(next.signal);
  next.abort();
  await assert.rejects(waiting, { name: "AbortError" });
});
