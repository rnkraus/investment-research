"use client";

import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import {
  ApiProtocolError,
  ApiRequestError,
  ApiTransportError,
  apiClient,
  requireLocalSourcePath,
  type ResearchModel,
  type DocumentCatalogEntry,
  type ResearchModelOptions,
} from "@/lib/api/client";

import { archiveCopy, fixtureDefaults } from "./copy";
import { RetrievalContext } from "./retrieval-context";
import { RejectionDiagnostic } from "./rejection-diagnostic";
import {
  applyResearchEvent,
  attachCitationSource,
  beginResearchSession,
  EMPTY_RESEARCH_SESSION,
  getResearchDisplaySteps,
  ResearchSequenceError,
  type ResearchSession,
} from "./research-session";

interface ResearchWorkflowProps {
  readonly documents?: readonly DocumentCatalogEntry[];
  readonly documentIds: readonly string[] | undefined;
  readonly onBusyChange: (busy: boolean) => void;
}

function safeErrorMessage(error: unknown): string {
  if (error instanceof ApiRequestError) {
    return error.requestId
      ? `${error.message} Request ID: ${error.requestId}`
      : error.message;
  }
  if (error instanceof ApiProtocolError || error instanceof ApiTransportError) {
    return error.message;
  }
  return archiveCopy.errors.unexpected;
}

function modelLabel(model: ResearchModel): string {
  return model === "gpt-5.6-terra"
    ? archiveCopy.research.terraModel
    : archiveCopy.research.lunaModel;
}

export function ResearchWorkflow({
  documents = [],
  documentIds,
  onBusyChange,
}: ResearchWorkflowProps) {
  const questionInputId = useId();
  const generationModelId = useId();
  const supportModelId = useId();
  const requestController = useRef<AbortController | null>(null);
  const [question, setQuestion] = useState<string>(
    fixtureDefaults.answerableQuestion,
  );
  const [session, setSession] = useState<ResearchSession>(
    EMPTY_RESEARCH_SESSION,
  );
  const [isBusy, setIsBusy] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string>();
  const [modelOptions, setModelOptions] = useState<ResearchModelOptions>();
  const [generationModel, setGenerationModel] =
    useState<ResearchModel>("gpt-5.6-terra");
  const [supportModel, setSupportModel] =
    useState<ResearchModel>("gpt-5.6-terra");
  const [isLoadingModelOptions, setIsLoadingModelOptions] = useState(true);
  const [modelOptionsFailed, setModelOptionsFailed] = useState(false);
  const [modelOptionsRequestId, setModelOptionsRequestId] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void apiClient
      .getResearchModelOptions(controller.signal)
      .then((options) => {
        if (controller.signal.aborted) {
          return;
        }
        setModelOptions(options);
        setGenerationModel((current) =>
          options.generation_models.includes(current)
            ? current
            : (options.generation_models[0] ?? "gpt-5.6-terra"),
        );
        setSupportModel((current) =>
          options.support_models.includes(current)
            ? current
            : (options.support_models[0] ?? "gpt-5.6-terra"),
        );
        setIsLoadingModelOptions(false);
      })
      .catch(() => {
        if (controller.signal.aborted) {
          return;
        }
        setModelOptionsFailed(true);
        setIsLoadingModelOptions(false);
      });
    return () => controller.abort();
  }, [modelOptionsRequestId]);

  useEffect(() => {
    return () => {
      const controller = requestController.current;
      requestController.current = null;
      controller?.abort();
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      documentIds === undefined ||
      modelOptions === undefined ||
      (modelOptions.provider_enabled &&
        (!modelOptions.generation_models.includes(generationModel) ||
          !modelOptions.support_models.includes(supportModel)))
    ) {
      return;
    }
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    let currentSession = beginResearchSession();
    setSession(currentSession);
    setIsBusy(true);
    onBusyChange(true);
    setErrorMessage(undefined);

    try {
      for await (const researchEvent of apiClient.streamResearch(
        {
          question,
          generation_model: modelOptions.provider_enabled
            ? generationModel
            : "gpt-5.6-terra",
          support_model: modelOptions.provider_enabled
            ? supportModel
            : "gpt-5.6-terra",
          context: {
            document_ids: documentIds,
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
            viewer_url: requireLocalSourcePath(source.viewer_url),
          });
          setSession(currentSession);
        }
      }
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        setSession({
          ...EMPTY_RESEARCH_SESSION,
          researchRequestId: currentSession.researchRequestId,
          retrievalContext: currentSession.retrievalContext,
        });
        setErrorMessage(safeErrorMessage(error));
      }
    } finally {
      if (requestController.current === controller) {
        requestController.current = null;
        setIsBusy(false);
        onBusyChange(false);
      }
    }
  }

  const visibleProgressSteps = getResearchDisplaySteps(session);
  const modelSelectionReady =
    modelOptions !== undefined &&
    (!modelOptions.provider_enabled ||
      (modelOptions.generation_models.includes(generationModel) &&
        modelOptions.support_models.includes(supportModel)));

  function retryModelOptions() {
    setModelOptions(undefined);
    setModelOptionsFailed(false);
    setIsLoadingModelOptions(true);
    setModelOptionsRequestId((current) => current + 1);
  }

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
          className="mt-6 block text-sm font-semibold text-slate-800"
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
        {isLoadingModelOptions ? (
          <p className="mt-5 text-sm text-slate-600" role="status">
            {archiveCopy.research.modelOptionsLoading}
          </p>
        ) : null}
        {modelOptionsFailed ? (
          <div className="mt-5 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950">
            <p role="alert">{archiveCopy.research.modelOptionsUnavailable}</p>
            <button
              className="mt-2 font-semibold underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700"
              onClick={retryModelOptions}
              type="button"
            >
              {archiveCopy.research.retryModelOptions}
            </button>
          </div>
        ) : null}
        {modelOptions?.provider_enabled === false ? (
          <p className="mt-5 text-sm text-slate-600">
            {archiveCopy.research.deterministicMode}
          </p>
        ) : null}
        {modelOptions?.provider_enabled === true ? (
          <fieldset className="mt-6 rounded-2xl border border-slate-200 p-4 sm:p-5">
            <legend className="px-1 text-sm font-semibold text-slate-900">
              {archiveCopy.research.modelSettingsLabel}
            </legend>
            <div className="grid gap-5 sm:grid-cols-2">
              <div>
                <label
                  className="block text-sm font-semibold text-slate-800"
                  htmlFor={generationModelId}
                >
                  {archiveCopy.research.generationModelLabel}
                </label>
                <p className="mt-1 text-xs text-slate-600">
                  {archiveCopy.research.generationModelHint}
                </p>
                <select
                  className="mt-2 block w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-950 focus:border-teal-600 focus:outline-none focus:ring-3 focus:ring-teal-100 disabled:opacity-50"
                  disabled={isBusy}
                  id={generationModelId}
                  onChange={(event) => {
                    const selected = modelOptions.generation_models.find(
                      (model) => model === event.target.value,
                    );
                    if (selected !== undefined) {
                      setGenerationModel(selected);
                    }
                  }}
                  value={generationModel}
                >
                  {modelOptions.generation_models.map((model) => (
                    <option key={model} value={model}>
                      {modelLabel(model)}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label
                  className="block text-sm font-semibold text-slate-800"
                  htmlFor={supportModelId}
                >
                  {archiveCopy.research.supportModelLabel}
                </label>
                <p className="mt-1 text-xs text-slate-600">
                  {archiveCopy.research.supportModelHint}
                </p>
                <select
                  className="mt-2 block w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-950 focus:border-teal-600 focus:outline-none focus:ring-3 focus:ring-teal-100 disabled:opacity-50"
                  disabled={isBusy}
                  id={supportModelId}
                  onChange={(event) => {
                    const selected = modelOptions.support_models.find(
                      (model) => model === event.target.value,
                    );
                    if (selected !== undefined) {
                      setSupportModel(selected);
                    }
                  }}
                  value={supportModel}
                >
                  {modelOptions.support_models.map((model) => (
                    <option key={model} value={model}>
                      {modelLabel(model)}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          </fieldset>
        ) : null}
        <button
          className="mt-5 inline-flex items-center justify-center rounded-xl bg-teal-700 px-5 py-3 text-sm font-semibold text-white shadow-sm transition hover:bg-teal-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 disabled:cursor-wait disabled:bg-slate-400"
          disabled={isBusy || !modelSelectionReady || documentIds === undefined}
          type="submit"
        >
          {isBusy
            ? archiveCopy.research.submitBusy
            : archiveCopy.research.submitIdle}
        </button>
      </form>

      {session.retrievalContext !== undefined ? (
        <RetrievalContext
          key={session.researchRequestId}
          sources={session.retrievalContext}
          documents={documents}
        />
      ) : null}

      <div aria-live="polite" className="mt-7">
        {session.status === "RUNNING" ? (
          <div className="rounded-2xl bg-sky-50 px-4 py-4 text-sm text-sky-950">
            <h3 className="font-semibold">
              {archiveCopy.research.progressHeading}
            </h3>
            {session.stage === undefined ? (
              <p className="mt-2" role="status">
                {archiveCopy.research.starting}
              </p>
            ) : (
              <ol className="mt-3 space-y-2" role="list">
                {visibleProgressSteps.map((step, index) => {
                  const completed = step.status === "COMPLETED";
                  const active = step.status === "ACTIVE";
                  return (
                    <li
                      aria-current={active ? "step" : undefined}
                      className={`flex items-start gap-3 ${active ? "font-semibold" : completed ? "text-sky-800" : "text-slate-500"}`}
                      key={step.stage}
                    >
                      <span
                        aria-hidden="true"
                        className={`flex size-6 shrink-0 items-center justify-center rounded-full text-xs ${active ? "bg-sky-800 text-white" : completed ? "bg-sky-200 text-sky-900" : "bg-white text-slate-500"}`}
                      >
                        {completed ? "✓" : index + 1}
                      </span>
                      <span>{archiveCopy.research.progress[step.stage]}</span>
                    </li>
                  );
                })}
              </ol>
            )}
          </div>
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
            {session.rejectionDiagnostic !== undefined ? (
              <RejectionDiagnostic
                diagnostic={session.rejectionDiagnostic}
                sources={session.retrievalContext ?? []}
                documents={documents}
              />
            ) : null}
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
            {session.coverage === "PARTIAL" ? (
              <p className="mt-4 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-950">
                {archiveCopy.research.partialCoverageNote}
              </p>
            ) : null}
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
                                {citation.source.locator.kind === "PDF_PAGE" ? (
                                  <>
                                    {archiveCopy.citation.pageLabel}{" "}
                                    {citation.source.locator.page}
                                  </>
                                ) : (
                                  <>
                                    {archiveCopy.citation.textRangeLabel}{" "}
                                    {citation.source.locator.start_character +
                                      1}
                                    {"–"}
                                    {citation.source.locator.end_character}
                                  </>
                                )}
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
