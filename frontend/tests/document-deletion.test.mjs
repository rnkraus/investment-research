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
  monitorDeletionBatch,
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

function trackingClock() {
  let elapsed = 0;
  return {
    now: () => elapsed,
    async wait(signal, delayMs) {
      signal.throwIfAborted();
      assert.ok(delayMs > 0 && delayMs <= 5_000);
      elapsed += delayMs;
    },
    advance(delayMs) {
      elapsed += delayMs;
    },
  };
}

test("automatic tracking continues beyond four rounds and stops on confirmed completion", async () => {
  const clock = trackingClock();
  const seen = [];
  const updates = [];
  let secondReads = 0;
  const result = await monitorDeletionBatch(
    beginDeletionBatch([FIRST, SECOND]),
    {
      async getDocumentDeletion(id) {
        seen.push(id);
        if (id === FIRST) return receipt(id, "COMPLETED");
        secondReads += 1;
        return receipt(id, secondReads === 6 ? "COMPLETED" : "PENDING");
      },
    },
    new AbortController().signal,
    (batch) => updates.push(batch),
    clock,
  );
  assert.equal(secondReads, 6);
  assert.equal(seen.filter((id) => id === FIRST).length, 1);
  assert.equal(updates.length, 6);
  assert.equal(clock.now(), 30_000);
  assert.equal(isDeletionComplete(result), true);
});

test("five-minute tracking deadline preserves pending counts and allows a later manual check", async () => {
  const clock = trackingClock();
  let calls = 0;
  const result = await monitorDeletionBatch(
    beginDeletionBatch([FIRST]),
    {
      async getDocumentDeletion(id) {
        calls += 1;
        return receipt(id);
      },
    },
    new AbortController().signal,
    () => {},
    clock,
  );
  assert.equal(clock.now(), 300_000);
  assert.equal(calls, 59);
  assert.equal(isDeletionComplete(result), false);
  assert.deepEqual(result.receipts, [receipt()]);
  const refreshed = await refreshDeletionBatch(
    result,
    {
      async getDocumentDeletion(id) {
        return receipt(id, "COMPLETED");
      },
    },
    new AbortController().signal,
  );
  assert.equal(isDeletionComplete(refreshed), true);
});

test("slow status reads count toward the same automatic tracking budget", async () => {
  const clock = trackingClock();
  let calls = 0;
  await monitorDeletionBatch(
    beginDeletionBatch([FIRST]),
    {
      async getDocumentDeletion(id) {
        calls += 1;
        clock.advance(8_000);
        return receipt(id);
      },
    },
    new AbortController().signal,
    () => {},
    clock,
  );
  assert.equal(calls, 23);
  assert.equal(clock.now(), 300_000);
});

test("the overall tracking deadline also cancels an in-flight read", async (t) => {
  const deadline = new AbortController();
  t.mock.method(AbortSignal, "timeout", (milliseconds) => {
    assert.ok(milliseconds === 300_000 || milliseconds === 10_000);
    return milliseconds === 300_000
      ? deadline.signal
      : new AbortController().signal;
  });
  const batch = acceptDeletionBatch(
    beginDeletionBatch([FIRST]),
    accepted({ items: [receipt()] }),
  );
  let calls = 0;
  const result = await monitorDeletionBatch(
    batch,
    {
      async getDocumentDeletion(id, signal) {
        calls += 1;
        deadline.abort(new DOMException("Tracking deadline", "TimeoutError"));
        assert.equal(signal.aborted, true);
        signal.throwIfAborted();
      },
    },
    new AbortController().signal,
    () => assert.fail("expired reads must not update the count"),
    trackingClock(),
  );
  assert.equal(calls, 1);
  assert.equal(result, batch);
  assert.equal(isDeletionComplete(result), false);
});

test("temporary read failures do not end tracking or imply completed deletion", async () => {
  const clock = trackingClock();
  const updates = [];
  let calls = 0;
  const result = await monitorDeletionBatch(
    beginDeletionBatch([FIRST]),
    {
      async getDocumentDeletion(id) {
        calls += 1;
        if (calls === 1) throw new TypeError("offline");
        return receipt(id, "COMPLETED");
      },
    },
    new AbortController().signal,
    (batch) => updates.push(batch),
    clock,
  );
  assert.deepEqual(updates[0].unavailableDocumentIds, [FIRST]);
  assert.equal(isDeletionComplete(updates[0]), false);
  assert.equal(isDeletionComplete(result), true);
  assert.equal(calls, 2);
});

test("cancelling background tracking prevents further reads and state updates", async () => {
  const controller = new AbortController();
  const clock = trackingClock();
  let reads = 0;
  let updates = 0;
  await assert.rejects(
    monitorDeletionBatch(
      beginDeletionBatch([FIRST]),
      {
        async getDocumentDeletion(id) {
          reads += 1;
          return receipt(id);
        },
      },
      controller.signal,
      () => {
        updates += 1;
        controller.abort();
      },
      clock,
    ),
    { name: "AbortError" },
  );
  assert.equal(reads, 1);
  assert.equal(updates, 1);
});

test("already completed batches never schedule automatic reads", async () => {
  const batch = acceptDeletionBatch(
    beginDeletionBatch([FIRST]),
    accepted({ items: [receipt(FIRST, "COMPLETED")] }),
  );
  const result = await monitorDeletionBatch(
    batch,
    {
      async getDocumentDeletion() {
        assert.fail("must not read");
      },
    },
    new AbortController().signal,
    () => assert.fail("must not update"),
    {
      now: () => 0,
      async wait() {
        assert.fail("must not wait");
      },
    },
  );
  assert.equal(result, batch);
});
