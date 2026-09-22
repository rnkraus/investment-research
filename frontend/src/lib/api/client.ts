import type { components } from "./generated/schema";

export type DocumentAccepted = components["schemas"]["DocumentAccepted"];
export type ProcessingRunResult = components["schemas"]["ProcessingRunResult"];
export type PastedTextDocumentInput =
  components["schemas"]["PastedTextDocumentInput"];
export type ResearchRequestInput =
  components["schemas"]["ResearchRequestInput"];
export type ResearchStreamEvent = components["schemas"]["ResearchStreamEvent"];
export type EvidenceSpanResult = components["schemas"]["EvidenceSpanResult"];
export type ApiErrorCode = components["schemas"]["ErrorCode"];

export type OriginalSource =
  | {
      readonly kind: "PDF";
      readonly content: Blob;
    }
  | {
      readonly kind: "TEXT";
      readonly content: string;
    };

export interface CaptureDocumentInput {
  readonly file: File;
  readonly title: string;
  readonly language?: string;
  readonly publishedAt?: string;
  readonly availableAt?: string;
}

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code?: ApiErrorCode;
  readonly requestId?: string;

  constructor(
    message: string,
    options: {
      readonly status: number;
      readonly code?: ApiErrorCode;
      readonly requestId?: string;
    },
  ) {
    super(message);
    this.name = "ApiRequestError";
    this.status = options.status;
    this.code = options.code;
    this.requestId = options.requestId;
  }
}

export class ApiProtocolError extends Error {
  constructor(message = "The API returned an invalid response.") {
    super(message);
    this.name = "ApiProtocolError";
  }
}

type FetchImplementation = typeof fetch;
type JsonRecord = Readonly<Record<string, unknown>>;
type JsonGuard<T> = (value: unknown) => value is T;

const API_PATH_PREFIX = "/api/v1/";
const SOURCE_PATH_PREFIX = "/sources/";
const SAFE_API_BASE = "http://local.invalid";
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const ERROR_CODES = new Set<ApiErrorCode>([
  "INVALID_REQUEST",
  "NOT_FOUND",
  "CONFLICT",
  "PAYLOAD_TOO_LARGE",
  "UNSUPPORTED_MEDIA_TYPE",
  "PROCESSING_FAILED",
  "EVIDENCE_VALIDATION_FAILED",
  "PROVIDER_UNAVAILABLE",
  "INTERNAL_ERROR",
]);
const PROCESSING_STATES = new Set([
  "CAPTURED",
  "PARSING",
  "LEXICAL_READY",
  "INDEXING",
  "SEARCHABLE",
  "FAILED",
]);
const RESEARCH_EVENTS = new Set([
  "research.progress",
  "answer.statement",
  "answer.citation",
  "answer.completed",
  "answer.insufficient",
  "research.failed",
]);

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}

function isDocumentAccepted(value: unknown): value is DocumentAccepted {
  return (
    isRecord(value) &&
    isUuid(value.document_id) &&
    isUuid(value.document_version_id) &&
    isUuid(value.processing_run_id) &&
    value.state === "CAPTURED" &&
    isNonEmptyString(value.processing_status_url)
  );
}

function isProcessingRunResult(value: unknown): value is ProcessingRunResult {
  return (
    isRecord(value) &&
    isUuid(value.processing_run_id) &&
    isUuid(value.document_version_id) &&
    typeof value.state === "string" &&
    PROCESSING_STATES.has(value.state) &&
    (value.error_summary === undefined ||
      value.error_summary === null ||
      isNonEmptyString(value.error_summary))
  );
}

function isEvidenceSpanResult(value: unknown): value is EvidenceSpanResult {
  return (
    isRecord(value) &&
    isUuid(value.evidence_span_id) &&
    isUuid(value.document_id) &&
    isUuid(value.document_version_id) &&
    isNonNegativeInteger(value.start_character) &&
    Number.isInteger(value.end_character) &&
    Number(value.end_character) > Number(value.start_character) &&
    isNonEmptyString(value.exact_text) &&
    typeof value.text_sha256 === "string" &&
    SHA256_PATTERN.test(value.text_sha256) &&
    isNonEmptyString(value.viewer_url) &&
    isEvidenceLocator(value.locator)
  );
}

function isEvidenceLocator(
  value: unknown,
): value is EvidenceSpanResult["locator"] {
  if (!isRecord(value)) {
    return false;
  }
  if (value.kind === "PDF_PAGE") {
    return Number.isInteger(value.page) && Number(value.page) >= 1;
  }
  if (value.kind === "TEXT_PASSAGE") {
    return (
      isNonNegativeInteger(value.start_character) &&
      Number.isInteger(value.end_character) &&
      Number(value.end_character) > Number(value.start_character)
    );
  }
  return false;
}

function isErrorCode(value: unknown): value is ApiErrorCode {
  return typeof value === "string" && ERROR_CODES.has(value as ApiErrorCode);
}

function isResearchStreamEvent(value: unknown): value is ResearchStreamEvent {
  if (
    !isRecord(value) ||
    value.schema_version !== "1.0" ||
    !isUuid(value.research_request_id) ||
    typeof value.event !== "string" ||
    !RESEARCH_EVENTS.has(value.event)
  ) {
    return false;
  }

  switch (value.event) {
    case "research.progress":
      return ["RETRIEVING", "GENERATING", "VALIDATING"].includes(
        String(value.stage),
      );
    case "answer.statement":
      return (
        isUuid(value.statement_id) &&
        isNonNegativeInteger(value.order) &&
        isNonEmptyString(value.text)
      );
    case "answer.citation":
      return (
        isUuid(value.statement_id) &&
        isUuid(value.evidence_span_id) &&
        ["SUPPORTS", "CONFLICTS"].includes(String(value.relationship)) &&
        isNonNegativeInteger(value.display_order)
      );
    case "answer.completed":
      return ["COMPLETE", "PARTIAL", "INSUFFICIENT", "CONFLICTING"].includes(
        String(value.coverage),
      );
    case "answer.insufficient":
      return (
        value.coverage === "INSUFFICIENT" && isNonEmptyString(value.message)
      );
    case "research.failed":
      return (
        isRecord(value.error) &&
        isErrorCode(value.error.code) &&
        isNonEmptyString(value.error.message) &&
        typeof value.retriable === "boolean"
      );
  }

  return false;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ApiProtocolError();
  }
}

async function requestError(response: Response): Promise<ApiRequestError> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    payload = undefined;
  }

  if (isRecord(payload) && isRecord(payload.error)) {
    const code = isErrorCode(payload.error.code)
      ? payload.error.code
      : undefined;
    const message = isNonEmptyString(payload.error.message)
      ? payload.error.message
      : "The request could not be completed.";
    const requestId = isUuid(payload.error.request_id)
      ? payload.error.request_id
      : undefined;
    return new ApiRequestError(message, {
      status: response.status,
      code,
      requestId,
    });
  }

  return new ApiRequestError("The request could not be completed.", {
    status: response.status,
  });
}

async function requestJson<T>(
  fetchImplementation: FetchImplementation,
  path: string,
  guard: JsonGuard<T>,
  init?: RequestInit,
): Promise<T> {
  const response = await fetchImplementation(path, init);
  if (!response.ok) {
    throw await requestError(response);
  }

  const payload = await readJson(response);
  if (!guard(payload)) {
    throw new ApiProtocolError();
  }
  return payload;
}

function parseServerSentEvent(frame: string): ResearchStreamEvent | undefined {
  let eventName: string | undefined;
  const dataLines: string[] = [];

  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) {
      eventName = line.slice("event:".length).trimStart();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }

  if (eventName === undefined && dataLines.length === 0) {
    return undefined;
  }
  if (eventName === undefined || dataLines.length === 0) {
    throw new ApiProtocolError();
  }

  let payload: unknown;
  try {
    payload = JSON.parse(dataLines.join("\n"));
  } catch {
    throw new ApiProtocolError();
  }
  if (!isResearchStreamEvent(payload) || payload.event !== eventName) {
    throw new ApiProtocolError();
  }
  return payload;
}

async function* decodeResearchStream(
  response: Response,
): AsyncGenerator<ResearchStreamEvent> {
  if (response.body === null) {
    throw new ApiProtocolError();
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    buffer = (buffer + decoder.decode(value, { stream: !done })).replaceAll(
      "\r\n",
      "\n",
    );

    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const event = parseServerSentEvent(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (event !== undefined) {
        yield event;
      }
      boundary = buffer.indexOf("\n\n");
    }

    if (done) {
      break;
    }
  }

  if (buffer.trim()) {
    const event = parseServerSentEvent(buffer);
    if (event !== undefined) {
      yield event;
    }
  }
}

export function requireLocalApiPath(value: string): string {
  if (!value.startsWith(API_PATH_PREFIX) || value.includes("\\")) {
    throw new ApiProtocolError("The API returned an unsafe local path.");
  }

  const parsed = new URL(value, SAFE_API_BASE);
  if (
    parsed.origin !== SAFE_API_BASE ||
    !parsed.pathname.startsWith(API_PATH_PREFIX)
  ) {
    throw new ApiProtocolError("The API returned an unsafe local path.");
  }
  return `${parsed.pathname}${parsed.search}${parsed.hash}`;
}

export function requireLocalSourcePath(value: string): string {
  if (!value.startsWith(SOURCE_PATH_PREFIX) || value.includes("\\")) {
    throw new ApiProtocolError("The API returned an unsafe source path.");
  }

  const parsed = new URL(value, SAFE_API_BASE);
  const evidenceSpanId = parsed.pathname.slice(SOURCE_PATH_PREFIX.length);
  if (
    parsed.origin !== SAFE_API_BASE ||
    parsed.search ||
    parsed.hash ||
    value !== parsed.pathname ||
    parsed.pathname !== `${SOURCE_PATH_PREFIX}${evidenceSpanId}` ||
    !isUuid(evidenceSpanId)
  ) {
    throw new ApiProtocolError("The API returned an unsafe source path.");
  }
  return parsed.pathname;
}

export function createApiClient(
  fetchImplementation: FetchImplementation = globalThis.fetch.bind(globalThis),
) {
  return {
    captureDocument(
      input: CaptureDocumentInput,
      signal?: AbortSignal,
    ): Promise<DocumentAccepted> {
      const body = new FormData();
      body.set("file", input.file);
      body.set("title", input.title);
      body.set("language", input.language ?? "en");
      if (input.publishedAt) {
        body.set("published_at", input.publishedAt);
      }
      if (input.availableAt) {
        body.set("available_at", input.availableAt);
      }

      return requestJson(
        fetchImplementation,
        "/api/v1/documents",
        isDocumentAccepted,
        { method: "POST", body, signal },
      );
    },

    capturePastedText(
      input: PastedTextDocumentInput,
      signal?: AbortSignal,
    ): Promise<DocumentAccepted> {
      return requestJson(
        fetchImplementation,
        "/api/v1/documents/text",
        isDocumentAccepted,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(input),
          signal,
        },
      );
    },

    getProcessingRun(
      processingStatusUrl: string,
      signal?: AbortSignal,
    ): Promise<ProcessingRunResult> {
      return requestJson(
        fetchImplementation,
        requireLocalApiPath(processingStatusUrl),
        isProcessingRunResult,
        { signal },
      );
    },

    async *streamResearch(
      input: ResearchRequestInput,
      signal?: AbortSignal,
    ): AsyncGenerator<ResearchStreamEvent> {
      const response = await fetchImplementation("/api/v1/research-requests", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(input),
        signal,
      });
      if (!response.ok) {
        throw await requestError(response);
      }

      yield* decodeResearchStream(response);
    },

    resolveEvidenceSpan(
      evidenceSpanId: string,
      signal?: AbortSignal,
    ): Promise<EvidenceSpanResult> {
      if (!isUuid(evidenceSpanId)) {
        return Promise.reject(
          new ApiProtocolError("The citation identifier is invalid."),
        );
      }
      return requestJson(
        fetchImplementation,
        `/api/v1/evidence-spans/${encodeURIComponent(evidenceSpanId)}`,
        isEvidenceSpanResult,
        { signal },
      );
    },

    async openOriginal(
      documentId: string,
      documentVersionId: string,
      signal?: AbortSignal,
    ): Promise<OriginalSource> {
      if (!isUuid(documentId) || !isUuid(documentVersionId)) {
        throw new ApiProtocolError("The source identifier is invalid.");
      }

      const response = await fetchImplementation(
        `/api/v1/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(documentVersionId)}/original`,
        { signal },
      );
      if (!response.ok) {
        throw await requestError(response);
      }

      const mediaType = response.headers
        .get("content-type")
        ?.split(";", 1)[0]
        .trim()
        .toLowerCase();
      if (mediaType === "application/pdf") {
        const content = await response.blob();
        if (content.size === 0) {
          throw new ApiProtocolError("The source document is empty.");
        }
        return { kind: "PDF", content };
      }
      if (mediaType === "text/plain") {
        return { kind: "TEXT", content: await response.text() };
      }
      throw new ApiProtocolError("The source media type is invalid.");
    },
  } as const;
}

export const apiClient = createApiClient();
