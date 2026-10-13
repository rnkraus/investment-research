"use client";

import {
  type FormEvent,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import {
  ApiProtocolError,
  ApiRequestError,
  apiClient,
  type DocumentAccepted,
  type ProcessingRunResult,
} from "@/lib/api/client";

import { archiveCopy, fixtureDefaults } from "./copy";
import { shouldPollProcessing } from "./processing-state";
import { getLocalPublicationDate } from "./publication-date";

const PROCESSING_POLL_INTERVAL_MS = 2_000;
type SourceInputMode = "pdf" | "text";

// Read the browser clock after hydration; the static server page has no user timezone.
function subscribeToLocalDate(): () => void {
  return () => {};
}

function getServerPublicationDate(): string {
  return "";
}

function processingMessage(result: ProcessingRunResult): string {
  switch (result.state) {
    case "CAPTURED":
      return archiveCopy.processing.captured;
    case "PARSING":
      return archiveCopy.processing.parsing;
    case "LEXICAL_READY":
      return archiveCopy.processing.lexicalReady;
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
    case "SEARCHABLE":
      return "border-emerald-300 bg-emerald-50 text-emerald-800";
    case "FAILED":
      return "border-rose-300 bg-rose-50 text-rose-800";
    case "PARSING":
    case "LEXICAL_READY":
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

interface DocumentWorkflowProps {
  readonly onArchiveChanged: () => void;
  readonly onClose: () => void;
}

export function DocumentWorkflow({
  onArchiveChanged,
  onClose,
}: DocumentWorkflowProps) {
  const fileInputId = useId();
  const textInputId = useId();
  const titleInputId = useId();
  const publicationDateInputId = useId();
  const uploadController = useRef<AbortController | null>(null);
  const [inputMode, setInputMode] = useState<SourceInputMode>("pdf");
  const [file, setFile] = useState<File>();
  const [text, setText] = useState("");
  const [title, setTitle] = useState<string>(fixtureDefaults.title);
  const today = useSyncExternalStore(
    subscribeToLocalDate,
    getLocalPublicationDate,
    getServerPublicationDate,
  );
  const [selectedPublicationDate, setPublicationDate] = useState<string>();
  const publicationDate = selectedPublicationDate ?? today;
  const [accepted, setAccepted] = useState<DocumentAccepted>();
  const [processing, setProcessing] = useState<ProcessingRunResult>();
  const [isUploading, setIsUploading] = useState(false);
  const [isChecking, setIsChecking] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string>();

  useEffect(() => {
    if (accepted !== undefined) {
      onArchiveChanged();
    }
  }, [accepted, processing?.state, onArchiveChanged]);

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
    if (accepted === undefined || !shouldPollProcessing(processing?.state)) {
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
      className="mb-6 rounded-2xl border border-slate-200 bg-white p-5 sm:p-6"
    >
      <div className="mb-5 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-slate-950">
            {archiveCopy.upload.heading}
          </h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-600">
            {archiveCopy.upload.description}
          </p>
        </div>
        <button
          className="shrink-0 rounded-lg border border-slate-300 px-3 py-2 text-sm font-semibold text-slate-700 hover:border-teal-600 focus-visible:outline-2 focus-visible:outline-teal-700"
          onClick={onClose}
          type="button"
        >
          {archiveCopy.workspace.closeImport}
        </button>
      </div>
      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <form className="min-w-0" onSubmit={handleSubmit}>
          <div className="space-y-5">
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

        <section className="rounded-xl border border-slate-200 bg-slate-50 p-5">
          <h3 className="text-sm font-semibold text-slate-950">
            {archiveCopy.processing.heading}
          </h3>

          {accepted === undefined || processing === undefined ? (
            <p className="mt-3 text-sm leading-6 text-slate-600">
              {archiveCopy.processing.idle}
            </p>
          ) : (
            <div className="mt-4" aria-live="polite">
              <span
                className={`inline-flex rounded-full border px-3 py-1 text-xs font-bold tracking-wide ${statusStyle(processing.state)}`}
              >
                {processing.state}
              </span>
              <p className="mt-3 text-sm leading-6 text-slate-700">
                {processingMessage(processing)}
              </p>
              <details className="mt-4 text-xs text-slate-600">
                <summary className="cursor-pointer font-semibold focus-visible:outline-2 focus-visible:outline-teal-700">
                  {archiveCopy.processing.runLabel}
                </summary>
                <dl className="mt-2">
                  <div>
                    <dt className="sr-only">
                      {archiveCopy.processing.runLabel}
                    </dt>
                    <dd className="break-all font-mono">
                      {processing.processing_run_id}
                    </dd>
                  </div>
                </dl>
              </details>

              {shouldPollProcessing(processing.state) ? (
                <div className="mt-6">
                  <p className="text-sm leading-6 text-slate-600">
                    {archiveCopy.processing.waiting}
                  </p>
                  <div className="mt-4">
                    <button
                      className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 hover:border-teal-600 focus-visible:outline-2 focus-visible:outline-teal-700 disabled:cursor-wait disabled:opacity-50"
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
      </div>
    </section>
  );
}
