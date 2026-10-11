"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { apiClient } from "@/lib/api/client";

import { archiveCopy } from "./copy";

type OriginalViewerState =
  | { readonly status: "LOADING" | "ERROR" }
  | { readonly status: "PDF"; readonly url: string }
  | { readonly status: "TEXT"; readonly text: string };

export function OriginalViewer({
  documentId,
  documentVersionId,
}: {
  readonly documentId: string;
  readonly documentVersionId: string;
}) {
  const [state, setState] = useState<OriginalViewerState>({
    status: "LOADING",
  });

  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | undefined;

    async function loadOriginal() {
      setState({ status: "LOADING" });
      try {
        // The same authorized, integrity-checking endpoint serves both catalog and citation views.
        const original = await apiClient.openOriginal(
          documentId,
          documentVersionId,
          controller.signal,
        );
        if (controller.signal.aborted) {
          return;
        }
        if (original.kind === "PDF") {
          objectUrl = URL.createObjectURL(original.content);
          setState({ status: "PDF", url: objectUrl });
        } else {
          setState({ status: "TEXT", text: original.content });
        }
      } catch {
        if (!controller.signal.aborted) {
          setState({ status: "ERROR" });
        }
      }
    }

    void loadOriginal();
    return () => {
      controller.abort();
      if (objectUrl !== undefined) {
        URL.revokeObjectURL(objectUrl);
      }
    };
  }, [documentId, documentVersionId]);

  return (
    <main className="min-h-screen px-5 py-6 sm:px-8 lg:px-12">
      <div className="mx-auto max-w-6xl">
        <header className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-200 pb-5">
          <div>
            <p className="text-xs font-bold tracking-[0.2em] text-teal-700 uppercase">
              {archiveCopy.catalog.originalEyebrow}
            </p>
            <h1 className="mt-2 text-2xl font-semibold tracking-tight text-slate-950">
              {archiveCopy.catalog.originalHeading}
            </h1>
          </div>
          <Link
            href="/"
            className="text-sm font-semibold text-teal-700 underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-teal-700"
          >
            {archiveCopy.source.backToArchive}
          </Link>
        </header>
        <p className="mt-5 text-sm text-slate-600">
          {archiveCopy.catalog.originalNotice}
        </p>
        <section aria-live="polite" className="py-8">
          {state.status === "LOADING" ? (
            <p role="status">{archiveCopy.catalog.originalLoading}</p>
          ) : null}
          {state.status === "ERROR" ? (
            <p
              role="alert"
              className="rounded-2xl border border-rose-200 bg-rose-50 p-5 text-sm text-rose-900"
            >
              {archiveCopy.catalog.originalUnavailable}
            </p>
          ) : null}
          {state.status === "PDF" ? (
            <div className="space-y-4">
              <h2 className="text-lg font-semibold text-slate-950">
                {archiveCopy.source.pdfHeading}
              </h2>
              <a
                href={state.url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-block text-sm font-semibold text-teal-700 underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-teal-700"
              >
                {archiveCopy.catalog.openPdf}
              </a>
              <iframe
                src={state.url}
                title={archiveCopy.source.pdfHeading}
                className="h-[72vh] w-full rounded-3xl border border-slate-300 bg-white"
              />
            </div>
          ) : null}
          {state.status === "TEXT" ? (
            <article className="rounded-3xl border border-slate-200 bg-white p-6">
              <h2 className="text-lg font-semibold text-slate-950">
                {archiveCopy.source.textHeading}
              </h2>
              <pre className="mt-6 overflow-x-auto rounded-2xl bg-slate-950 p-5 font-mono text-sm leading-7 whitespace-pre-wrap text-slate-100">
                {state.text}
              </pre>
            </article>
          ) : null}
        </section>
      </div>
    </main>
  );
}
