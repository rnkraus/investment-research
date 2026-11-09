"use client";

import { type FormEvent, useState } from "react";

import {
  ApiRequestError,
  apiClient,
  type DocumentCatalogEntry,
  type DocumentSourceAttribution,
} from "@/lib/api/client";

import { archiveCopy } from "./copy";
import { readSourceAttribution } from "./source-attribution";
import { SourceAttributionFields } from "./source-attribution-fields";

export function DocumentSourceAttributionEditor({
  document,
  onSaved,
}: {
  readonly document: DocumentCatalogEntry;
  readonly onSaved: (attribution: DocumentSourceAttribution) => void;
}) {
  const [status, setStatus] = useState<
    "idle" | "saving" | "saved" | "stale" | "failed"
  >("idle");
  const copy = archiveCopy.catalog;

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (status === "saving" || status === "stale") return;
    const fields = readSourceAttribution(event.currentTarget);
    setStatus("saving");
    try {
      const attribution = await apiClient.replaceDocumentSourceAttribution(
        document.document_id,
        { ...fields, document_version_id: document.document_version_id },
      );
      onSaved(attribution);
      setStatus("saved");
    } catch (error) {
      const stale =
        error instanceof ApiRequestError &&
        (error.status === 404 || error.status === 409);
      setStatus(stale ? "stale" : "failed");
    }
  }

  return (
    <details className="mt-2 text-xs">
      <summary className="cursor-pointer font-semibold text-teal-700 underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-teal-700">
        {copy.editSourceAttribution}
      </summary>
      <form
        className="mt-2 rounded-lg border border-slate-200 p-3"
        onSubmit={(event) => void save(event)}
        onChange={() => {
          if (status === "saved" || status === "failed") setStatus("idle");
        }}
      >
        <fieldset
          className="space-y-3"
          disabled={status === "saving" || status === "stale"}
        >
          <SourceAttributionFields initial={document} />
          <button
            type="submit"
            className="rounded-lg border border-slate-300 px-3 py-2 font-semibold text-teal-700 focus-visible:outline-2 focus-visible:outline-teal-700 disabled:opacity-50"
          >
            {status === "saving"
              ? copy.savingSourceAttribution
              : copy.saveSourceAttribution}
          </button>
        </fieldset>
        {status === "saved" && (
          <p role="status" className="mt-2">
            {copy.sourceAttributionSaved}
          </p>
        )}
        {(status === "stale" || status === "failed") && (
          <p role="alert" className="mt-2 text-rose-700">
            {status === "stale"
              ? copy.sourceAttributionStale
              : copy.sourceAttributionFailed}
          </p>
        )}
      </form>
    </details>
  );
}
