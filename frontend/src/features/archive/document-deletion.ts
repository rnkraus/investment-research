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
  client: {
    getDocumentDeletion(
      id: string,
      signal?: AbortSignal,
    ): Promise<DocumentDeletionResult>;
  },
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

export function waitForDeletionCheck(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    function cancel() {
      clearTimeout(timer);
      reject(signal.reason);
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", cancel);
      resolve();
    }, 2_000);
    signal.addEventListener("abort", cancel, { once: true });
  });
}
