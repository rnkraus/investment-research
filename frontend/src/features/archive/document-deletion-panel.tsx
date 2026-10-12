"use client";

import { useEffect, useId, useRef, useState } from "react";

import {
  ApiRequestError,
  apiClient,
  type DocumentCatalogEntry,
} from "@/lib/api/client";

import { archiveCopy } from "./copy";
import {
  acceptDeletionBatch,
  beginDeletionBatch,
  isDeletionComplete,
  refreshDeletionBatch,
  waitForDeletionCheck,
  type DeletionBatch,
} from "./document-deletion";

const buttonClass =
  "rounded-xl border px-4 py-2 text-sm font-semibold focus-visible:outline-2 focus-visible:outline-teal-700 disabled:opacity-50";

export function DocumentDeletionPanel({
  documents,
  disabled,
  onBusyChange,
  onArchiveChanged,
}: {
  readonly documents: readonly DocumentCatalogEntry[];
  readonly disabled: boolean;
  readonly onBusyChange: (busy: boolean) => void;
  readonly onArchiveChanged: () => void;
}) {
  const headingId = useId();
  const hintId = useId();
  const [confirmation, setConfirmation] = useState<
    readonly DocumentCatalogEntry[] | null
  >(null);
  const [batch, setBatch] = useState<DeletionBatch | null>(null);
  const [busy, setBusy] = useState(false);
  const [rejected, setRejected] = useState(false);
  const operation = useRef<AbortController | null>(null);

  useEffect(() => () => operation.current?.abort(), []);

  async function monitor(
    current: DeletionBatch,
    signal: AbortSignal,
    rounds: number,
  ) {
    let updated = current;
    for (
      let round = 0;
      round < rounds && !isDeletionComplete(updated);
      round += 1
    ) {
      if (round > 0) await waitForDeletionCheck(signal);
      updated = await refreshDeletionBatch(updated, apiClient, signal);
      signal.throwIfAborted();
      setBatch(updated);
    }
  }

  async function run(current: DeletionBatch, submit: boolean) {
    if (operation.current !== null) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(true);
    onBusyChange(true);
    setRejected(false);
    setConfirmation(null);
    setBatch(current);
    if (submit) onArchiveChanged();
    try {
      let updated = current;
      if (submit) {
        // Recheck an uncertain previous attempt before publishing another confirmed batch.
        if (current.receipts.length > 0 || batch !== null) {
          updated = await refreshDeletionBatch(
            current,
            apiClient,
            controller.signal,
          );
          setBatch(updated);
        }
        if (!isDeletionComplete(updated)) {
          try {
            const accepted = await apiClient.deleteDocuments(
              {
                document_ids: updated.documentIds,
                confirm_permanent: true,
              },
              AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]),
            );
            controller.signal.throwIfAborted();
            updated = acceptDeletionBatch(updated, accepted);
            setBatch(updated);
          } catch (error) {
            controller.signal.throwIfAborted();
            if (
              error instanceof ApiRequestError &&
              [404, 409, 422].includes(error.status)
            ) {
              setRejected(true);
              setBatch(updated.receipts.length > 0 ? updated : null);
              return;
            }
            // Transport, schema, and 5xx failures leave the commit outcome uncertain.
            // Read surviving receipts instead of claiming the operation rolled back.
          }
        }
      }
      await monitor(updated, controller.signal, submit ? 4 : 1);
    } catch {
      // Keep the last confirmed snapshots; cancellation or failed reads are not completion.
    } finally {
      if (!controller.signal.aborted) {
        if (submit) onArchiveChanged();
        setBusy(false);
        onBusyChange(false);
        operation.current = null;
      }
    }
  }

  const completedCount =
    batch?.receipts.filter((item) => item.state === "COMPLETED").length ?? 0;
  const complete = batch !== null && isDeletionComplete(batch);

  return (
    <div className="mt-3">
      <button
        type="button"
        disabled={
          disabled ||
          documents.length === 0 ||
          confirmation !== null ||
          batch !== null ||
          busy
        }
        aria-describedby={hintId}
        onClick={() => {
          setConfirmation([...documents]);
          setRejected(false);
          onBusyChange(true);
        }}
        className={`${buttonClass} border-rose-300 text-rose-700`}
      >
        {archiveCopy.catalog.deleteSelected}
      </button>
      <p id={hintId} className="mt-2 text-xs text-slate-500">
        {archiveCopy.catalog.deletionHint}
      </p>

      {confirmation !== null ? (
        <section
          aria-labelledby={headingId}
          className="mt-3 rounded-xl border border-rose-300 bg-rose-50 p-4"
        >
          <h3 id={headingId} className="font-semibold text-rose-900">
            {archiveCopy.deletion.heading}
          </h3>
          <p className="mt-2 text-sm text-rose-900">
            {archiveCopy.deletion.warning}
          </p>
          <p className="mt-2 text-xs text-slate-700">
            {archiveCopy.deletion.boundary}
          </p>
          <p className="mt-2 text-xs text-slate-700">
            {archiveCopy.deletion.trackingLifetime}
          </p>
          <p className="mt-3 text-sm font-semibold">
            {archiveCopy.deletion.selection}: {confirmation.length}
          </p>
          <ul className="mt-2 max-h-40 list-inside list-disc overflow-auto text-sm">
            {confirmation.map((item) => (
              <li key={item.document_id} className="break-words">
                {item.title}
              </li>
            ))}
          </ul>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              className={`${buttonClass} border-rose-700 bg-rose-700 text-white`}
              onClick={() =>
                void run(
                  beginDeletionBatch(
                    confirmation.map((item) => item.document_id),
                  ),
                  true,
                )
              }
            >
              {archiveCopy.deletion.confirm}
            </button>
            <button
              type="button"
              disabled={busy}
              className={`${buttonClass} border-slate-300 text-slate-700`}
              onClick={() => {
                setConfirmation(null);
                onBusyChange(false);
              }}
            >
              {archiveCopy.deletion.cancel}
            </button>
          </div>
        </section>
      ) : null}

      <div aria-live="polite" aria-atomic="true">
        {rejected ? (
          <p role="alert" className="mt-3 text-sm text-rose-700">
            {archiveCopy.deletion.rejected}
          </p>
        ) : null}
        {batch !== null ? (
          <section
            aria-label={archiveCopy.deletion.tracking}
            className="mt-3 rounded-xl border border-slate-300 p-4 text-sm"
          >
            <p className="font-semibold">
              {archiveCopy.deletion.progress}: {completedCount} /{" "}
              {batch.documentIds.length}
            </p>
            <p className="mt-2">
              {complete
                ? archiveCopy.deletion.completed
                : batch.unavailableDocumentIds.length > 0
                  ? archiveCopy.deletion.uncertain
                  : archiveCopy.deletion.pending}
            </p>
            {batch.dispatchPendingDocumentIds.length > 0 ? (
              <p className="mt-2 text-amber-800">
                {archiveCopy.deletion.dispatchPending}
              </p>
            ) : null}
            <p className="mt-2 text-xs text-slate-500">
              {archiveCopy.deletion.clearedAnswer}
            </p>
            {!complete ? (
              <p className="mt-2 text-xs text-slate-500">
                {archiveCopy.deletion.trackingLifetime}
              </p>
            ) : null}
            {busy ? (
              <p role="status" className="mt-2">
                {archiveCopy.deletion.checking}
              </p>
            ) : null}
            <div className="mt-3 flex flex-wrap gap-2">
              {complete ? (
                <button
                  type="button"
                  disabled={busy}
                  className={`${buttonClass} border-slate-300`}
                  onClick={() => setBatch(null)}
                >
                  {archiveCopy.deletion.close}
                </button>
              ) : (
                <>
                  <button
                    type="button"
                    disabled={busy || disabled}
                    className={`${buttonClass} border-slate-300`}
                    onClick={() => void run(batch, false)}
                  >
                    {archiveCopy.deletion.check}
                  </button>
                  <button
                    type="button"
                    disabled={busy || disabled}
                    className={`${buttonClass} border-rose-300 text-rose-700`}
                    onClick={() => void run(batch, true)}
                  >
                    {archiveCopy.deletion.retry}
                  </button>
                </>
              )}
            </div>
          </section>
        ) : null}
      </div>
    </div>
  );
}
