"use client";

import { useEffect, useId, useRef, useState } from "react";

import {
  apiClient,
  type DocumentCatalogEntry,
  type DocumentListCursor,
} from "@/lib/api/client";

import { archiveCopy } from "./copy";
import {
  DOCUMENT_SELECTION_LIMIT,
  EMPTY_DOCUMENT_SELECTION,
  getResearchDocumentIds,
  resetDocumentSelection,
  toggleDocumentSelection,
  type DocumentSelection,
} from "./document-selection";
import { ResearchWorkflow } from "./research-workflow";

function displayDate(value: string | null | undefined): string {
  // Keep publication, capture, and availability distinct; never infer missing dates.
  return value == null
    ? archiveCopy.catalog.unknownDate
    : new Date(value).toISOString().slice(0, 10);
}

export function ArchiveResearch({
  refreshKey,
}: {
  readonly refreshKey: string;
}) {
  const scopeName = useId();
  const [documents, setDocuments] = useState<readonly DocumentCatalogEntry[]>(
    [],
  );
  const [cursor, setCursor] = useState<DocumentListCursor | null>();
  const [selection, setSelection] = useState<DocumentSelection>(
    EMPTY_DOCUMENT_SELECTION,
  );
  const [refreshCount, setRefreshCount] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [isResearching, setIsResearching] = useState(false);
  const [failed, setFailed] = useState(false);
  const [selectionWasReset, setSelectionWasReset] = useState(false);
  const request = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;

    async function loadFirstPage() {
      setIsLoading(true);
      setFailed(false);
      setDocuments([]);
      setCursor(undefined);
      setSelectionWasReset(true);
      setSelection(resetDocumentSelection);
      try {
        const page = await apiClient.listDocuments(
          undefined,
          controller.signal,
        );
        if (!controller.signal.aborted) {
          setDocuments(page.items);
          setCursor(page.next_cursor);
        }
      } catch {
        if (!controller.signal.aborted) {
          setFailed(true);
        }
      } finally {
        if (!controller.signal.aborted) {
          setIsLoading(false);
        }
      }
    }

    void loadFirstPage();
    return () => {
      controller.abort();
      request.current?.abort();
    };
  }, [refreshKey, refreshCount]);

  async function loadMore() {
    if (cursor == null || isLoading) {
      return;
    }
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    setIsLoading(true);
    setFailed(false);
    try {
      const page = await apiClient.listDocuments(cursor, controller.signal);
      if (!controller.signal.aborted) {
        setDocuments((current) => {
          const merged = new Map(
            current.map((item) => [item.document_id, item]),
          );
          for (const item of page.items) {
            merged.set(item.document_id, item);
          }
          return [...merged.values()];
        });
        setCursor(page.next_cursor);
      }
    } catch {
      if (!controller.signal.aborted) {
        setFailed(true);
      }
    } finally {
      if (!controller.signal.aborted) {
        setIsLoading(false);
      }
    }
  }

  const documentIds = getResearchDocumentIds(selection);
  const researchDocumentIds =
    selection.scope === "SELECTED" && (isLoading || failed)
      ? undefined
      : documentIds;

  return (
    <div>
      <section
        aria-labelledby="catalog-heading"
        className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm sm:p-8"
      >
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="text-xs font-bold tracking-[0.2em] text-teal-700 uppercase">
              {archiveCopy.catalog.eyebrow}
            </p>
            <h2
              id="catalog-heading"
              className="mt-3 text-2xl font-semibold tracking-tight text-slate-950"
            >
              {archiveCopy.catalog.heading}
            </h2>
            <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-600">
              {archiveCopy.catalog.description}
            </p>
          </div>
          <button
            type="button"
            disabled={isLoading || isResearching}
            onClick={() => setRefreshCount((current) => current + 1)}
            className="rounded-xl border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 hover:border-teal-600 focus-visible:outline-2 focus-visible:outline-teal-700 disabled:opacity-50"
          >
            {archiveCopy.catalog.refresh}
          </button>
        </div>

        <fieldset disabled={isResearching} className="mt-6">
          <legend className="text-sm font-semibold text-slate-800">
            {archiveCopy.research.scopeLegend}
          </legend>
          <div className="mt-2 flex flex-wrap gap-4 text-sm text-slate-700">
            {(["ALL", "SELECTED"] as const).map((scope) => (
              <label
                key={scope}
                className="inline-flex cursor-pointer items-center gap-2"
              >
                <input
                  type="radio"
                  name={scopeName}
                  checked={selection.scope === scope}
                  onChange={() => {
                    setSelection((current) => ({
                      scope,
                      documents: scope === "ALL" ? [] : current.documents,
                    }));
                    setSelectionWasReset(false);
                  }}
                />
                {scope === "ALL"
                  ? archiveCopy.research.archiveScope
                  : archiveCopy.research.documentScope}
              </label>
            ))}
          </div>
          <p className="mt-2 text-xs text-slate-500">
            {archiveCopy.research.scopeHint}
          </p>
          <p className="mt-2 text-xs text-slate-500">
            {archiveCopy.catalog.selectionHint}
          </p>

          <ul className="mt-5 divide-y divide-slate-200">
            {documents.map((document) => {
              const selected = selection.documents.some(
                (item) => item.document_id === document.document_id,
              );
              const selectable = document.processing?.state === "SEARCHABLE";
              const label = `${archiveCopy.catalog.selectDocument}: ${document.title}`;
              return (
                <li
                  key={document.document_id}
                  className="flex items-start gap-3 py-4"
                >
                  <input
                    type="checkbox"
                    aria-label={label}
                    checked={selected && selection.scope === "SELECTED"}
                    disabled={
                      isLoading ||
                      !selectable ||
                      (!selected &&
                        selection.documents.length >= DOCUMENT_SELECTION_LIMIT)
                    }
                    onChange={() => {
                      setSelection((current) =>
                        toggleDocumentSelection(current, document),
                      );
                      setSelectionWasReset(false);
                    }}
                    className="mt-1 size-4 shrink-0 accent-teal-700"
                  />
                  <div className="min-w-0 flex-1">
                    <h3 className="font-semibold break-words text-slate-900">
                      {document.title}
                    </h3>
                    <p className="mt-1 text-xs text-slate-600">
                      {document.media_type === "application/pdf"
                        ? "PDF"
                        : "Text"}{" "}
                      · {document.language} ·{" "}
                      {document.byte_size.toLocaleString("en-US")} bytes
                    </p>
                    <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600">
                      {(
                        [
                          [
                            archiveCopy.catalog.published,
                            document.published_at,
                          ],
                          [archiveCopy.catalog.captured, document.captured_at],
                          [
                            archiveCopy.catalog.available,
                            document.available_at,
                          ],
                        ] as const
                      ).map(([name, value]) => (
                        <div key={name} className="flex gap-1">
                          <dt>{name}:</dt>
                          <dd>{displayDate(value)}</dd>
                        </div>
                      ))}
                    </dl>
                    <p
                      className={`mt-2 text-xs font-semibold ${selectable ? "text-emerald-700" : "text-slate-600"}`}
                    >
                      {document.processing === null
                        ? archiveCopy.catalog.notProcessed
                        : archiveCopy.catalog.statuses[
                            document.processing.state
                          ]}
                    </p>
                    {document.processing?.state === "FAILED" &&
                    document.processing.error_summary ? (
                      <p className="mt-1 text-xs text-rose-700">
                        {document.processing.error_summary}
                      </p>
                    ) : null}
                  </div>
                  <a
                    href={`/documents/${document.document_id}/versions/${document.document_version_id}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="shrink-0 text-sm font-semibold text-teal-700 underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-teal-700"
                    aria-label={`${archiveCopy.catalog.openOriginal}: ${document.title}`}
                  >
                    {archiveCopy.catalog.openOriginal}
                  </a>
                </li>
              );
            })}
          </ul>
        </fieldset>

        <div aria-live="polite" className="mt-3 text-sm text-slate-600">
          {isLoading ? (
            <p role="status">{archiveCopy.catalog.loading}</p>
          ) : null}
          {failed ? (
            <p role="alert" className="text-rose-700">
              {archiveCopy.catalog.unavailable}
            </p>
          ) : null}
          {!isLoading && !failed && documents.length === 0 ? (
            <p>{archiveCopy.catalog.empty}</p>
          ) : null}
          {selection.scope === "SELECTED" ? (
            <p className="mt-2">
              {archiveCopy.catalog.selected}: {selection.documents.length}.{" "}
              {documentIds === undefined
                ? archiveCopy.catalog.selectionRequired
                : ""}
            </p>
          ) : null}
          {selectionWasReset && selection.scope === "SELECTED" ? (
            <p className="mt-2">{archiveCopy.catalog.selectionReset}</p>
          ) : null}
          {selection.documents.length >= DOCUMENT_SELECTION_LIMIT ? (
            <p className="mt-2">{archiveCopy.catalog.selectionLimit}</p>
          ) : null}
        </div>
        {cursor != null ? (
          <button
            type="button"
            disabled={isLoading || isResearching}
            onClick={() => void loadMore()}
            className="mt-4 rounded-xl border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 focus-visible:outline-2 focus-visible:outline-teal-700 disabled:opacity-50"
          >
            {archiveCopy.catalog.loadMore}
          </button>
        ) : null}
      </section>
      <ResearchWorkflow
        documentIds={researchDocumentIds}
        onBusyChange={setIsResearching}
      />
    </div>
  );
}
