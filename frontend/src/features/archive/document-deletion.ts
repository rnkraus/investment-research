import type {
  DocumentDeletionResult,
  DocumentDeletionsAccepted,
} from "../../lib/api/client";

export interface DeletionBatch {
  readonly documentIds: readonly string[];
  readonly receipts: readonly DocumentDeletionResult[];
  readonly dispatchPendingDocumentIds: readonly string[];
  readonly unavailableDocumentIds: readonly string[];
}

interface DeletionStatusClient {
  getDocumentDeletion(
    id: string,
    signal?: AbortSignal,
  ): Promise<DocumentDeletionResult>;
}

const AUTOMATIC_TRACKING_MS = 5 * 60_000;
const STATUS_CHECK_INTERVAL_MS = 5_000;

export function beginDeletionBatch(
  documentIds: readonly string[],
): DeletionBatch {
  return {
    documentIds: [...documentIds],
    receipts: [],
    dispatchPendingDocumentIds: [],
    unavailableDocumentIds: [...documentIds],
  };
}

export function acceptDeletionBatch(
  batch: DeletionBatch,
  accepted: DocumentDeletionsAccepted,
): DeletionBatch {
  return {
    ...batch,
    receipts: accepted.items,
    dispatchPendingDocumentIds: accepted.dispatch_pending_document_ids,
    unavailableDocumentIds: [],
  };
}

export function isDeletionComplete(batch: DeletionBatch): boolean {
  return (
    batch.documentIds.length > 0 &&
    batch.documentIds.every((id) =>
      batch.receipts.some(
        (item) => item.document_id === id && item.state === "COMPLETED",
      ),
    )
  );
}

/** Read only explicit targets; a missing or unreachable receipt never proves completion. */
export async function refreshDeletionBatch(
  batch: DeletionBatch,
  client: DeletionStatusClient,
  signal: AbortSignal,
): Promise<DeletionBatch> {
  const receipts = new Map(
    batch.receipts.map((item) => [item.document_id, item]),
  );
  const pending = batch.documentIds.filter(
    (id) => receipts.get(id)?.state !== "COMPLETED",
  );
  const unavailable: string[] = [];
  // Bound concurrency and the entire read round, including a large confirmed batch.
  const readSignal = AbortSignal.any([signal, AbortSignal.timeout(10_000)]);
  for (let index = 0; index < pending.length; index += 5) {
    await Promise.all(
      pending.slice(index, index + 5).map(async (id) => {
        try {
          readSignal.throwIfAborted();
          const receipt = await client.getDocumentDeletion(id, readSignal);
          receipts.set(id, receipt);
        } catch {
          unavailable.push(id);
        }
      }),
    );
    signal.throwIfAborted();
  }
  return {
    ...batch,
    receipts: batch.documentIds.flatMap((id) => {
      const receipt = receipts.get(id);
      return receipt ? [receipt] : [];
    }),
    dispatchPendingDocumentIds: batch.dispatchPendingDocumentIds.filter(
      (id) => receipts.get(id)?.state !== "COMPLETED",
    ),
    unavailableDocumentIds: unavailable,
  };
}

/** Background tracking reads receipts only; it never submits or retries deletion. */
export async function monitorDeletionBatch(
  batch: DeletionBatch,
  client: DeletionStatusClient,
  signal: AbortSignal,
  onUpdate: (updated: DeletionBatch) => void,
  timing = {
    now: () => performance.now(),
    wait: waitForDeletionCheck,
  },
): Promise<DeletionBatch> {
  signal.throwIfAborted();
  if (isDeletionComplete(batch)) return batch;
  const deadline = timing.now() + AUTOMATIC_TRACKING_MS;
  const timeout = AbortSignal.timeout(AUTOMATIC_TRACKING_MS);
  const trackingSignal = AbortSignal.any([signal, timeout]);
  let updated = batch;
  try {
    while (!isDeletionComplete(updated)) {
      trackingSignal.throwIfAborted();
      const remaining = deadline - timing.now();
      if (remaining <= 0) break;
      await timing.wait(
        trackingSignal,
        Math.min(STATUS_CHECK_INTERVAL_MS, remaining),
      );
      trackingSignal.throwIfAborted();
      if (timing.now() >= deadline) break;
      updated = await refreshDeletionBatch(updated, client, trackingSignal);
      trackingSignal.throwIfAborted();
      onUpdate(updated);
    }
  } catch (error) {
    signal.throwIfAborted();
    if (!timeout.aborted) throw error;
  }
  return updated;
}

export function waitForDeletionCheck(
  signal: AbortSignal,
  delayMs = STATUS_CHECK_INTERVAL_MS,
): Promise<void> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    function cancel() {
      clearTimeout(timer);
      reject(signal.reason);
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", cancel);
      resolve();
    }, delayMs);
    signal.addEventListener("abort", cancel, { once: true });
  });
}
