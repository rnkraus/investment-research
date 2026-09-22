"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import {
  ApiProtocolError,
  apiClient,
  requireLocalSourcePath,
} from "@/lib/api/client";

import { archiveCopy } from "./copy";
import { type VerifiedTextAnchor, verifyTextAnchor } from "./source-anchor";

interface SourceViewerProps {
  readonly evidenceSpanId: string;
}

type SourceViewerState =
  | { readonly status: "LOADING" }
  | { readonly status: "ERROR" }
  | {
      readonly status: "PDF";
      readonly exactText: string;
      readonly page: number;
      readonly viewerUrl: string;
    }
  | {
      readonly status: "TEXT";
      readonly anchor: VerifiedTextAnchor;
      readonly startCharacter: number;
      readonly endCharacter: number;
    };

export function SourceViewer({ evidenceSpanId }: SourceViewerProps) {
  const [state, setState] = useState<SourceViewerState>({ status: "LOADING" });

  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | undefined;

    async function loadSource() {
      setState({ status: "LOADING" });
      try {
        const source = await apiClient.resolveEvidenceSpan(
          evidenceSpanId,
          controller.signal,
        );
        const sourcePath = requireLocalSourcePath(source.viewer_url);
        if (sourcePath !== `/sources/${evidenceSpanId}`) {
          throw new ApiProtocolError("The citation source does not match.");
        }

        const original = await apiClient.openOriginal(
          source.document_id,
          source.document_version_id,
          controller.signal,
        );
        if (controller.signal.aborted) {
          return;
        }

        if (source.locator.kind === "PDF_PAGE") {
          if (original.kind !== "PDF") {
            throw new ApiProtocolError("The source media type does not match.");
          }
          objectUrl = URL.createObjectURL(original.content);
          setState({
            status: "PDF",
            exactText: source.exact_text,
            page: source.locator.page,
            viewerUrl: `${objectUrl}#page=${source.locator.page}`,
          });
          return;
        }

        if (original.kind !== "TEXT") {
          throw new ApiProtocolError("The source media type does not match.");
        }
        const anchor = await verifyTextAnchor({
          original: original.content,
          startCharacter: source.locator.start_character,
          endCharacter: source.locator.end_character,
          exactText: source.exact_text,
          textSha256: source.text_sha256,
        });
        if (!controller.signal.aborted) {
          setState({
            status: "TEXT",
            anchor,
            startCharacter: source.locator.start_character,
            endCharacter: source.locator.end_character,
          });
        }
      } catch {
        if (!controller.signal.aborted) {
          setState({ status: "ERROR" });
        }
      }
    }

    void loadSource();
    return () => {
      controller.abort();
      if (objectUrl !== undefined) {
        URL.revokeObjectURL(objectUrl);
      }
    };
  }, [evidenceSpanId]);

  return (
    <main className="min-h-screen px-5 py-6 sm:px-8 lg:px-12">
      <div className="mx-auto max-w-6xl">
        <header className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-200 pb-5">
          <div>
            <p className="text-xs font-bold tracking-[0.2em] text-teal-700 uppercase">
              {archiveCopy.source.eyebrow}
            </p>
            <h1 className="mt-2 text-2xl font-semibold tracking-tight text-slate-950 sm:text-3xl">
              {archiveCopy.source.heading}
            </h1>
          </div>
          <Link
            className="text-sm font-semibold text-teal-700 underline decoration-teal-300 underline-offset-4 hover:text-teal-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-600"
            href="/"
          >
            {archiveCopy.source.backToArchive}
          </Link>
        </header>

        <section aria-live="polite" className="py-8">
          {state.status === "LOADING" ? (
            <p className="rounded-2xl border border-sky-200 bg-sky-50 p-5 text-sm text-sky-900">
              {archiveCopy.source.loading}
            </p>
          ) : null}

          {state.status === "ERROR" ? (
            <p
              className="rounded-2xl border border-rose-200 bg-rose-50 p-5 text-sm text-rose-900"
              role="alert"
            >
              {archiveCopy.source.unavailable}
            </p>
          ) : null}

          {state.status === "PDF" ? (
            <div className="space-y-6">
              <article className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm shadow-slate-200/70">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <h2 className="text-lg font-semibold text-slate-950">
                    {archiveCopy.source.pdfHeading}
                  </h2>
                  <span
                    className="rounded-full bg-teal-50 px-3 py-1 text-xs font-bold text-teal-800"
                    data-pdf-page={state.page}
                  >
                    {archiveCopy.source.pageLabel} {state.page}
                  </span>
                </div>
                <p className="mt-5 text-xs font-bold tracking-wide text-slate-500 uppercase">
                  {archiveCopy.source.exactEvidence}
                </p>
                <blockquote className="mt-2 border-l-2 border-teal-600 pl-4 text-sm leading-6 text-slate-700">
                  {state.exactText}
                </blockquote>
              </article>
              <iframe
                className="h-[72vh] w-full rounded-3xl border border-slate-300 bg-white"
                src={state.viewerUrl}
                title={`${archiveCopy.source.pdfHeading}, ${archiveCopy.source.pageLabel} ${state.page}`}
              />
            </div>
          ) : null}

          {state.status === "TEXT" ? (
            <article className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm shadow-slate-200/70 sm:p-8">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h2 className="text-lg font-semibold text-slate-950">
                  {archiveCopy.source.textHeading}
                </h2>
                <span className="rounded-full bg-teal-50 px-3 py-1 text-xs font-bold text-teal-800">
                  {archiveCopy.source.textRangeLabel} {state.startCharacter + 1}
                  {"–"}
                  {state.endCharacter}
                </span>
              </div>
              <pre className="mt-6 overflow-x-auto rounded-2xl bg-slate-950 p-5 font-mono text-sm leading-7 whitespace-pre-wrap text-slate-100">
                {state.anchor.before}
                <mark
                  className="rounded-sm bg-amber-300 px-0.5 text-slate-950"
                  data-testid="source-highlight"
                >
                  {state.anchor.exact}
                </mark>
                {state.anchor.after}
              </pre>
            </article>
          ) : null}
        </section>
      </div>
    </main>
  );
}
