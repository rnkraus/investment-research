"use client";

import {
  type FormEvent,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

import {
  ApiProtocolError,
  ApiRequestError,
  apiClient,
  type DocumentAccepted,
  type ProcessingRunResult,
} from "@/lib/api/client";

import { archiveCopy, fixtureDefaults } from "./copy";
import { ResearchWorkflow } from "./research-workflow";

const PROCESSING_POLL_INTERVAL_MS = 2_000;
type SourceInputMode = "pdf" | "text";

function processingMessage(result: ProcessingRunResult): string {
  switch (result.state) {
    case "CAPTURED":
      return archiveCopy.processing.captured;
    case "PARSING":
      return archiveCopy.processing.parsing;
    case "LEXICAL_READY":
      return archiveCopy.processing.ready;
    case "INDEXING":
      return archiveCopy.processing.indexing;
    case "SEARCHABLE":
      return archiveCopy.processing.ready;
    case "FAILED":
      return result.error_summary ?? archiveCopy.processing.failed;
  }
}

function statusStyle(state: ProcessingRunResult["state"]): string {
  switch (state) {
    case "LEXICAL_READY":
    case "SEARCHABLE":
      return "border-emerald-300 bg-emerald-50 text-emerald-800";
    case "FAILED":
      return "border-rose-300 bg-rose-50 text-rose-800";
    case "PARSING":
    case "INDEXING":
      return "border-sky-300 bg-sky-50 text-sky-800";
    case "CAPTURED":
      return "border-amber-300 bg-amber-50 text-amber-900";
  }
}

function safeErrorMessage(error: unknown): string {
  if (error instanceof ApiRequestError) {
    return error.message;
  }
  if (error instanceof ApiProtocolError) {
    return archiveCopy.errors.unexpected;
  }
  return archiveCopy.errors.unexpected;
}

export function DocumentWorkflow() {
  const fileInputId = useId();
  const textInputId = useId();
  const titleInputId = useId();
  const publicationDateInputId = useId();
  const uploadController = useRef<AbortController | null>(null);
  const [inputMode, setInputMode] = useState<SourceInputMode>("pdf");
  const [file, setFile] = useState<File>();
  const [text, setText] = useState("");
  const [title, setTitle] = useState<string>(fixtureDefaults.title);
  const [publicationDate, setPublicationDate] = useState("2026-02-19");
  const [accepted, setAccepted] = useState<DocumentAccepted>();
  const [processing, setProcessing] = useState<ProcessingRunResult>();
  const [isUploading, setIsUploading] = useState(false);
  const [isChecking, setIsChecking] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string>();

  useEffect(() => {
    return () => {
      const controller = uploadController.current;
      uploadController.current = null;
      controller?.abort();
    };
  }, []);

  const refreshStatus = useCallback(
    async (signal?: AbortSignal): Promise<boolean> => {
      if (accepted === undefined) {
        return false;
      }

      setIsChecking(true);
      try {
        const result = await apiClient.getProcessingRun(
          accepted.processing_status_url,
          signal,
        );
        setProcessing(result);
        setErrorMessage(undefined);
        return true;
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          return false;
        }
        setErrorMessage(safeErrorMessage(error));
        return false;
      } finally {
        if (!signal?.aborted) {
          setIsChecking(false);
        }
      }
    },
    [accepted],
  );

  useEffect(() => {
    if (
      accepted === undefined ||
      processing?.state === "LEXICAL_READY" ||
      processing?.state === "SEARCHABLE" ||
      processing?.state === "FAILED"
    ) {
      return;
    }

    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      const succeeded = await refreshStatus(controller.signal);
      if (succeeded && !controller.signal.aborted) {
        timer = setTimeout(poll, PROCESSING_POLL_INTERVAL_MS);
      }
    };
    timer = setTimeout(poll, PROCESSING_POLL_INTERVAL_MS);

    return () => {
      controller.abort();
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    };
  }, [accepted, processing?.state, refreshStatus]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inputMode === "pdf" && file === undefined) {
      setErrorMessage(archiveCopy.errors.missingFile);
      return;
    }
    if (inputMode === "text" && text.trim().length === 0) {
      setErrorMessage(archiveCopy.errors.missingText);
      return;
    }

    uploadController.current?.abort();
    const controller = new AbortController();
    uploadController.current = controller;
    setIsUploading(true);
    setAccepted(undefined);
    setProcessing(undefined);
    setErrorMessage(undefined);

    try {
      const publishedAt = publicationDate
        ? `${publicationDate}T00:00:00Z`
        : undefined;
      let result: DocumentAccepted;
      if (inputMode === "pdf") {
        if (file === undefined) {
          throw new ApiProtocolError();
        }
        result = await apiClient.captureDocument(
          {
            file,
            title,
            language: fixtureDefaults.language,
            publishedAt,
          },
          controller.signal,
        );
      } else {
        result = await apiClient.capturePastedText(
          {
            title,
            text,
            language: fixtureDefaults.language,
            published_at: publishedAt,
          },
          controller.signal,
        );
      }
      setAccepted(result);
      setProcessing({
        processing_run_id: result.processing_run_id,
        document_version_id: result.document_version_id,
        state: "CAPTURED",
        error_summary: null,
      });
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        setErrorMessage(safeErrorMessage(error));
      }
    } finally {
      if (uploadController.current === controller) {
        uploadController.current = null;
        setIsUploading(false);
      }
    }
  }

  return (
    <section
      aria-label="Document import and processing"
      className="grid gap-6 lg:grid-cols-[1.08fr_0.92fr]"
    >
      <form
        className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm shadow-slate-200/70 sm:p-8"
        onSubmit={handleSubmit}
      >
        <p className="text-xs font-bold tracking-[0.2em] text-teal-700 uppercase">
          {archiveCopy.upload.eyebrow}
        </p>
        <h2 className="mt-3 text-2xl font-semibold tracking-tight text-slate-950">
          {archiveCopy.upload.heading}
        </h2>
        <p className="mt-3 max-w-xl text-sm leading-6 text-slate-600">
          {archiveCopy.upload.description}
        </p>

        <div className="mt-8 space-y-5">
          <fieldset disabled={isUploading}>
            <legend className="text-sm font-semibold text-slate-800">
              {archiveCopy.upload.modeLegend}
            </legend>
            <div className="mt-2 grid grid-cols-2 gap-2 rounded-2xl bg-slate-100 p-1">
              {(
                [
                  ["pdf", archiveCopy.upload.pdfMode],
                  ["text", archiveCopy.upload.textMode],
                ] as const
              ).map(([mode, label]) => (
                <label className="cursor-pointer" key={mode}>
                  <input
                    checked={inputMode === mode}
                    className="peer sr-only"
                    name="source-type"
                    onChange={() => {
                      setInputMode(mode);
                      setErrorMessage(undefined);
                    }}
                    type="radio"
                    value={mode}
                  />
                  <span className="block rounded-xl px-4 py-2.5 text-center text-sm font-semibold text-slate-600 transition hover:text-slate-900 peer-checked:bg-white peer-checked:text-slate-950 peer-checked:shadow-sm peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-teal-600">
                    {label}
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          {inputMode === "pdf" ? (
            <div>
              <label
                className="text-sm font-semibold text-slate-800"
                htmlFor={fileInputId}
              >
                {archiveCopy.upload.fileLabel}
              </label>
              <input
                accept=".pdf,application/pdf"
                className="mt-2 block w-full rounded-2xl border border-dashed border-slate-300 bg-slate-50 px-4 py-4 text-sm text-slate-700 file:mr-4 file:rounded-full file:border-0 file:bg-slate-900 file:px-4 file:py-2 file:text-sm file:font-semibold file:text-white hover:border-teal-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-600"
                id={fileInputId}
                name="document"
                onChange={(event) => {
                  setFile(event.target.files?.[0]);
                  setErrorMessage(undefined);
                }}
                required
                type="file"
              />
              <p className="mt-2 text-xs text-slate-500">
                {archiveCopy.upload.fileHint}
              </p>
            </div>
          ) : (
            <div>
              <label
                className="text-sm font-semibold text-slate-800"
                htmlFor={textInputId}
              >
                {archiveCopy.upload.textLabel}
              </label>
              <textarea
                className="mt-2 block min-h-48 w-full resize-y rounded-2xl border border-slate-300 bg-white px-4 py-3 text-sm leading-6 text-slate-950 shadow-xs outline-none placeholder:text-slate-400 focus:border-teal-600 focus:ring-3 focus:ring-teal-100"
                id={textInputId}
                maxLength={100_000}
                onChange={(event) => {
                  setText(event.target.value);
                  setErrorMessage(undefined);
                }}
                placeholder={archiveCopy.upload.textPlaceholder}
                required
                value={text}
              />
              <p className="mt-2 text-xs text-slate-500">
                {archiveCopy.upload.textHint}
              </p>
            </div>
          )}

          <div>
            <label
              className="text-sm font-semibold text-slate-800"
              htmlFor={titleInputId}
            >
              {archiveCopy.upload.titleLabel}
            </label>
            <input
              className="mt-2 block w-full rounded-xl border border-slate-300 bg-white px-4 py-3 text-sm text-slate-950 shadow-xs outline-none placeholder:text-slate-400 focus:border-teal-600 focus:ring-3 focus:ring-teal-100"
              id={titleInputId}
              maxLength={300}
              onChange={(event) => setTitle(event.target.value)}
              placeholder={archiveCopy.upload.titlePlaceholder}
              required
              type="text"
              value={title}
            />
          </div>

          <div>
            <label
              className="text-sm font-semibold text-slate-800"
              htmlFor={publicationDateInputId}
            >
              {archiveCopy.upload.publicationDateLabel}
            </label>
            <input
              className="mt-2 block w-full rounded-xl border border-slate-300 bg-white px-4 py-3 text-sm text-slate-950 shadow-xs outline-none focus:border-teal-600 focus:ring-3 focus:ring-teal-100"
              id={publicationDateInputId}
              onChange={(event) => setPublicationDate(event.target.value)}
              type="date"
              value={publicationDate}
            />
          </div>
        </div>

        {inputMode === "pdf" && file !== undefined ? (
          <p className="mt-5 rounded-xl bg-slate-100 px-4 py-3 text-sm text-slate-700">
            <span className="font-semibold">
              {archiveCopy.upload.selectedFile}:
            </span>{" "}
            {file.name}
          </p>
        ) : null}

        {errorMessage !== undefined ? (
          <p
            className="mt-5 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"
            role="alert"
          >
            {errorMessage}
          </p>
        ) : null}

        <button
          className="mt-6 inline-flex w-full items-center justify-center rounded-xl bg-teal-700 px-5 py-3 text-sm font-semibold text-white shadow-sm transition hover:bg-teal-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 disabled:cursor-not-allowed disabled:bg-slate-400 sm:w-auto"
          disabled={isUploading}
          type="submit"
        >
          {isUploading
            ? archiveCopy.upload.submitBusy
            : archiveCopy.upload.submitIdle}
        </button>
      </form>

      <section className="rounded-3xl bg-slate-950 p-6 text-white shadow-xl shadow-slate-300/30 sm:p-8">
        <p className="text-xs font-bold tracking-[0.2em] text-teal-300 uppercase">
          {archiveCopy.processing.eyebrow}
        </p>
        <h2 className="mt-3 text-2xl font-semibold tracking-tight">
          {archiveCopy.processing.heading}
        </h2>

        {accepted === undefined || processing === undefined ? (
          <p className="mt-5 text-sm leading-6 text-slate-300">
            {archiveCopy.processing.idle}
          </p>
        ) : (
          <div className="mt-6" aria-live="polite">
            <span
              className={`inline-flex rounded-full border px-3 py-1 text-xs font-bold tracking-wide ${statusStyle(processing.state)}`}
            >
              {processing.state}
            </span>
            <p className="mt-4 text-sm leading-6 text-slate-300">
              {processingMessage(processing)}
            </p>
            <dl className="mt-5 border-y border-slate-800 py-4">
              <div>
                <dt className="text-xs font-semibold tracking-wide text-slate-400 uppercase">
                  {archiveCopy.processing.runLabel}
                </dt>
                <dd className="mt-1 break-all font-mono text-xs text-slate-200">
                  {processing.processing_run_id}
                </dd>
              </div>
            </dl>

            {processing.state !== "LEXICAL_READY" &&
            processing.state !== "SEARCHABLE" &&
            processing.state !== "FAILED" ? (
              <div className="mt-6">
                <p className="text-sm leading-6 text-slate-300">
                  {archiveCopy.processing.waiting}
                </p>
                <div className="mt-4">
                  <button
                    className="rounded-xl bg-white px-4 py-2 text-sm font-semibold text-slate-950 transition hover:bg-teal-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-300 disabled:cursor-wait disabled:bg-slate-400"
                    disabled={isChecking}
                    onClick={() => void refreshStatus()}
                    type="button"
                  >
                    {isChecking
                      ? archiveCopy.processing.checking
                      : archiveCopy.processing.refresh}
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        )}
      </section>

      {accepted !== undefined &&
      (processing?.state === "LEXICAL_READY" ||
        processing?.state === "SEARCHABLE") ? (
        <div className="lg:col-span-2">
          <ResearchWorkflow
            documentId={accepted.document_id}
            key={accepted.document_id}
          />
        </div>
      ) : null}
    </section>
  );
}
