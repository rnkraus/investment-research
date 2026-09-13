"use client";

import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import {
  ApiRequestError,
  apiClient,
  requireLocalApiPath,
} from "@/lib/api/client";

import { archiveCopy, fixtureDefaults } from "./copy";
import {
  applyResearchEvent,
  attachCitationSource,
  beginResearchSession,
  EMPTY_RESEARCH_SESSION,
  ResearchSequenceError,
  type ResearchSession,
} from "./research-session";

interface ResearchWorkflowProps {
  readonly documentId: string;
}

function safeErrorMessage(error: unknown): string {
  if (error instanceof ApiRequestError) {
    return error.message;
  }
  return archiveCopy.errors.unexpected;
}

function progressMessage(stage: ResearchSession["stage"]): string | undefined {
  if (stage === undefined) {
    return undefined;
  }
  return archiveCopy.research.progress[stage];
}

export function ResearchWorkflow({ documentId }: ResearchWorkflowProps) {
  const questionInputId = useId();
  const requestController = useRef<AbortController | null>(null);
  const [question, setQuestion] = useState<string>(
    fixtureDefaults.answerableQuestion,
  );
  const [session, setSession] = useState<ResearchSession>(
    EMPTY_RESEARCH_SESSION,
  );
  const [isBusy, setIsBusy] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string>();

  useEffect(() => {
    return () => {
      const controller = requestController.current;
      requestController.current = null;
      controller?.abort();
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    let currentSession = beginResearchSession();
    setSession(currentSession);
    setIsBusy(true);
    setErrorMessage(undefined);

    try {
      for await (const researchEvent of apiClient.streamResearch(
        {
          question,
          context: {
            document_ids: [documentId],
            temporal_mode: "CURRENT",
          },
        },
        controller.signal,
      )) {
        currentSession = applyResearchEvent(currentSession, researchEvent);
        setSession(currentSession);
      }

      if (currentSession.status === "RUNNING") {
        throw new ResearchSequenceError();
      }

      if (currentSession.status === "COMPLETED") {
        const evidenceSpanIds = new Set(
          currentSession.statements.flatMap((statement) =>
            statement.citations.map((citation) => citation.evidenceSpanId),
          ),
        );
        for (const evidenceSpanId of evidenceSpanIds) {
          const source = await apiClient.resolveEvidenceSpan(
            evidenceSpanId,
            controller.signal,
          );
          currentSession = attachCitationSource(currentSession, {
            ...source,
            viewer_url: requireLocalApiPath(source.viewer_url),
          });
          setSession(currentSession);
        }
      }
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        setSession(EMPTY_RESEARCH_SESSION);
        setErrorMessage(safeErrorMessage(error));
      }
    } finally {
      if (requestController.current === controller) {
        requestController.current = null;
        setIsBusy(false);
      }
    }
  }

  const activeProgress = progressMessage(session.stage);

  return (
    <section
      aria-labelledby="research-heading"
      className="mt-8 rounded-3xl border border-slate-200 bg-white p-6 shadow-sm shadow-slate-200/70 sm:p-8"
    >
      <p className="text-xs font-bold tracking-[0.2em] text-teal-700 uppercase">
        {archiveCopy.research.eyebrow}
      </p>
      <h2
        className="mt-3 text-2xl font-semibold tracking-tight text-slate-950"
        id="research-heading"
      >
        {archiveCopy.research.heading}
      </h2>
      <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-600">
        {archiveCopy.research.description}
      </p>

      <form className="mt-7" onSubmit={handleSubmit}>
        <label
          className="text-sm font-semibold text-slate-800"
          htmlFor={questionInputId}
        >
          {archiveCopy.research.questionLabel}
        </label>
        <textarea
          className="mt-2 block min-h-28 w-full resize-y rounded-2xl border border-slate-300 bg-white px-4 py-3 text-sm leading-6 text-slate-950 shadow-xs outline-none placeholder:text-slate-400 focus:border-teal-600 focus:ring-3 focus:ring-teal-100"
          disabled={isBusy}
          id={questionInputId}
          maxLength={2_000}
          onChange={(event) => setQuestion(event.target.value)}
          required
          value={question}
        />
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            className="rounded-full border border-slate-300 px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:border-teal-600 hover:text-teal-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-600 disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isBusy}
            onClick={() => setQuestion(fixtureDefaults.answerableQuestion)}
            type="button"
          >
            {archiveCopy.research.answerableExample}
          </button>
          <button
            className="rounded-full border border-slate-300 px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:border-teal-600 hover:text-teal-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-600 disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isBusy}
            onClick={() => setQuestion(fixtureDefaults.unanswerableQuestion)}
            type="button"
          >
            {archiveCopy.research.unanswerableExample}
          </button>
        </div>
        <button
          className="mt-5 inline-flex items-center justify-center rounded-xl bg-teal-700 px-5 py-3 text-sm font-semibold text-white shadow-sm transition hover:bg-teal-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 disabled:cursor-wait disabled:bg-slate-400"
          disabled={isBusy}
          type="submit"
        >
          {isBusy
            ? archiveCopy.research.submitBusy
            : archiveCopy.research.submitIdle}
        </button>
      </form>

      <div aria-live="polite" className="mt-7">
        {activeProgress !== undefined ? (
          <p className="rounded-2xl bg-sky-50 px-4 py-3 text-sm text-sky-900">
            {activeProgress}
          </p>
        ) : null}

        {errorMessage !== undefined ? (
          <p
            className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"
            role="alert"
          >
            {errorMessage}
          </p>
        ) : null}

        {session.status === "INSUFFICIENT" ? (
          <article className="rounded-2xl border border-amber-200 bg-amber-50 p-5 text-amber-950">
            <h3 className="font-semibold">
              {archiveCopy.research.insufficientLabel}
            </h3>
            <p className="mt-2 text-sm leading-6">{session.message}</p>
          </article>
        ) : null}

        {session.status === "FAILED" ? (
          <article className="rounded-2xl border border-rose-200 bg-rose-50 p-5 text-rose-950">
            <h3 className="font-semibold">
              {archiveCopy.research.failedLabel}
            </h3>
            <p className="mt-2 text-sm leading-6">{session.message}</p>
          </article>
        ) : null}

        {session.status === "COMPLETED" ? (
          <article>
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 pb-4">
              <h3 className="text-lg font-semibold text-slate-950">
                {archiveCopy.research.resultLabel}
              </h3>
              <span className="rounded-full bg-emerald-100 px-3 py-1 text-xs font-bold text-emerald-800">
                {archiveCopy.research.coverageLabel}: {session.coverage}
              </span>
            </div>
            <ol className="mt-5 space-y-6">
              {session.statements.map((statement) => (
                <li key={statement.statementId}>
                  <p className="text-base leading-7 text-slate-900">
                    {statement.text}
                  </p>
                  <ol className="mt-3 space-y-3">
                    {statement.citations.map((citation) => (
                      <li
                        className="rounded-2xl border border-slate-200 bg-slate-50 p-4"
                        key={`${statement.statementId}-${citation.evidenceSpanId}`}
                      >
                        <p className="text-xs font-bold tracking-wide text-teal-700 uppercase">
                          {archiveCopy.citation.eyebrow} ·{" "}
                          {citation.relationship === "SUPPORTS"
                            ? archiveCopy.citation.supports
                            : archiveCopy.citation.conflicts}
                        </p>
                        {citation.source === undefined ? (
                          <p className="mt-2 text-sm text-slate-600">
                            {archiveCopy.citation.resolving}
                          </p>
                        ) : (
                          <>
                            <blockquote className="mt-3 border-l-2 border-teal-600 pl-4 text-sm leading-6 text-slate-700">
                              {citation.source.exact_text}
                            </blockquote>
                            <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
                              <span className="text-xs font-semibold text-slate-500">
                                {archiveCopy.citation.pageLabel}{" "}
                                {citation.source.page}
                              </span>
                              <a
                                className="text-sm font-semibold text-teal-700 underline decoration-teal-300 underline-offset-4 hover:text-teal-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-600"
                                href={citation.source.viewer_url}
                                rel="noopener noreferrer"
                                target="_blank"
                              >
                                {archiveCopy.citation.openOriginal}
                              </a>
                            </div>
                          </>
                        )}
                      </li>
                    ))}
                  </ol>
                </li>
              ))}
            </ol>
          </article>
        ) : null}
      </div>
    </section>
  );
}
