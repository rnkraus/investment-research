import type { components } from "./generated/schema";

export type DocumentAccepted = components["schemas"]["DocumentAccepted"];
export type DocumentCatalogEntry =
  components["schemas"]["DocumentCatalogEntry"];
export type DocumentCatalogPage = components["schemas"]["DocumentCatalogPage"];
export type DocumentListCursor = components["schemas"]["DocumentListCursor"];
export type DocumentDeletionInput =
  components["schemas"]["DocumentDeletionInput"];
export type DocumentDeletionResult =
  components["schemas"]["DocumentDeletionResult"];
export type DocumentDeletionsAccepted =
  components["schemas"]["DocumentDeletionsAccepted"];
export type ProcessingRunResult = components["schemas"]["ProcessingRunResult"];
export type PastedTextDocumentInput =
  components["schemas"]["PastedTextDocumentInput"];
export type ResearchRequestInput =
  components["schemas"]["ResearchRequestInput"];
export type ResearchModel = components["schemas"]["ResearchModel"];
export type ResearchModelOptions =
  components["schemas"]["ResearchModelOptions"];
export type ResearchStreamEvent = components["schemas"]["ResearchStreamEvent"];
export type RetrievalContextSource =
  components["schemas"]["AnswerGenerationSource"];
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

export class ApiTransportError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ApiTransportError";
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
  "RESEARCH_CONTEXT_LIMIT",
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
  "research.context",
  "research.progress",
  "answer.statement",
  "answer.citation",
  "answer.completed",
  "answer.insufficient",
  "research.failed",
]);
const RESEARCH_MODELS = new Set<ResearchModel>([
  "gpt-5.6-terra",
  "gpt-5.6-luna",
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

function isResearchModel(value: unknown): value is ResearchModel {
  return (
    typeof value === "string" && RESEARCH_MODELS.has(value as ResearchModel)
  );
}

function isResearchModelOptions(value: unknown): value is ResearchModelOptions {
  if (
    !isRecord(value) ||
    typeof value.provider_enabled !== "boolean" ||
    !Array.isArray(value.generation_models) ||
    !Array.isArray(value.support_models) ||
    !value.generation_models.every(isResearchModel) ||
    !value.support_models.every(isResearchModel)
  ) {
    return false;
  }

  if (!value.provider_enabled) {
    return (
      value.generation_models.length === 0 && value.support_models.length === 0
    );
  }
  return (
    value.generation_models.length > 0 &&
    value.support_models.length > 0 &&
    new Set(value.generation_models).size === value.generation_models.length &&
    new Set(value.support_models).size === value.support_models.length
  );
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

function isTimestamp(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(
      value,
    ) &&
    Number.isFinite(Date.parse(value))
  );
}

function isOptionalTimestamp(value: unknown): boolean {
  return value === undefined || value === null || isTimestamp(value);
}

function isDocumentDeletionResult(
  value: unknown,
): value is DocumentDeletionResult {
  if (
    !isRecord(value) ||
    Object.keys(value).length !== 5 ||
    !isUuid(value.document_id) ||
    !isTimestamp(value.requested_at) ||
    value.status_url !== `/api/v1/documents/${value.document_id}/deletion`
  ) {
    return false;
  }
  return (
    (value.state === "PENDING" && value.completed_at === null) ||
    (value.state === "COMPLETED" &&
      isTimestamp(value.completed_at) &&
      Date.parse(value.completed_at) >= Date.parse(value.requested_at))
  );
}

function isDocumentDeletionsAccepted(
  value: unknown,
): value is DocumentDeletionsAccepted {
  return (
    isRecord(value) &&
    Object.keys(value).length === 2 &&
    Array.isArray(value.items) &&
    value.items.length > 0 &&
    value.items.length <= 100 &&
    value.items.every(isDocumentDeletionResult) &&
    Array.isArray(value.dispatch_pending_document_ids) &&
    value.dispatch_pending_document_ids.every(isUuid)
  );
}

function isDocumentListCursor(value: unknown): value is DocumentListCursor {
  return (
    isRecord(value) &&
    isTimestamp(value.created_at) &&
    isUuid(value.document_id)
  );
}

function isDocumentCatalogEntry(value: unknown): value is DocumentCatalogEntry {
  return (
    isRecord(value) &&
    isUuid(value.document_id) &&
    isUuid(value.document_version_id) &&
    isNonEmptyString(value.title) &&
    Array.from(value.title).length <= 300 &&
    isNonEmptyString(value.language) &&
    ["application/pdf", "text/plain"].includes(String(value.media_type)) &&
    Number.isSafeInteger(value.byte_size) &&
    Number(value.byte_size) > 0 &&
    isTimestamp(value.created_at) &&
    isTimestamp(value.captured_at) &&
    isOptionalTimestamp(value.published_at) &&
    isOptionalTimestamp(value.available_at) &&
    (value.processing === null ||
      (isProcessingRunResult(value.processing) &&
        value.processing.document_version_id === value.document_version_id)) &&
    value.original_url ===
      `/api/v1/documents/${value.document_id}/versions/${value.document_version_id}/original`
  );
}

function isDocumentCatalogPage(value: unknown): value is DocumentCatalogPage {
  if (
    !isRecord(value) ||
    !Array.isArray(value.items) ||
    value.items.length > 100 ||
    !value.items.every(isDocumentCatalogEntry) ||
    new Set(value.items.map((item) => item.document_id)).size !==
      value.items.length
  ) {
    return false;
  }
  if (value.next_cursor === undefined || value.next_cursor === null) {
    return true;
  }
  const last = value.items.at(-1);
  return (
    isDocumentListCursor(value.next_cursor) &&
    last !== undefined &&
    value.next_cursor.document_id === last.document_id &&
    value.next_cursor.created_at === last.created_at
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

function isRetrievalContextSource(
  value: unknown,
): value is RetrievalContextSource {
  if (!isRecord(value)) {
    return false;
  }
  const fields = new Set([
    "retrieval_candidate_id",
    "chunk_id",
    "document_id",
    "document_version_id",
    "structure_block_id",
    "source_locator_kind",
    "page_number",
    "text",
  ]);
  return (
    Object.keys(value).every((key) => fields.has(key)) &&
    isUuid(value.retrieval_candidate_id) &&
    isUuid(value.chunk_id) &&
    isUuid(value.document_id) &&
    isUuid(value.document_version_id) &&
    isUuid(value.structure_block_id) &&
    typeof value.text === "string" &&
    value.text.trim().length > 0 &&
    Array.from(value.text).length <= 24_000 &&
    ((value.source_locator_kind === "PDF_PAGE" &&
      Number.isInteger(value.page_number) &&
      Number(value.page_number) >= 1) ||
      (value.source_locator_kind === "TEXT_PASSAGE" &&
        (value.page_number === null || value.page_number === undefined)))
  );
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
    case "research.context": {
      const sources = value.sources;
      return (
        Object.keys(value).every((key) =>
          [
            "event",
            "schema_version",
            "research_request_id",
            "sources",
          ].includes(key),
        ) &&
        Array.isArray(sources) &&
        sources.length <= 32 &&
        sources.every(isRetrievalContextSource) &&
        sources.reduce(
          (total, source) => total + Array.from(source.text).length,
          0,
        ) <= 24_000 &&
        new Set(sources.map((source) => source.chunk_id)).size ===
          sources.length &&
        new Set(sources.map((source) => source.retrieval_candidate_id)).size ===
          sources.length
      );
    }
    case "research.progress":
      return [
        "EMBEDDING_QUESTION",
        "SEARCHING_SOURCES",
        "SELECTING_PASSAGES",
        "GENERATING_ANSWER",
        "VALIDATING_EVIDENCE",
        "SAVING_RESULT",
      ].includes(String(value.stage));
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

async function requestError(
  response: Response,
  fallbackMessage = `The request could not be completed (HTTP ${response.status}).`,
): Promise<ApiRequestError> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    payload = undefined;
  }

  const headerRequestId = response.headers.get("x-request-id");
  const safeHeaderRequestId = isUuid(headerRequestId)
    ? headerRequestId
    : undefined;

  if (isRecord(payload) && isRecord(payload.error)) {
    const code = isErrorCode(payload.error.code)
      ? payload.error.code
      : undefined;
    const message =
      code !== undefined && isNonEmptyString(payload.error.message)
        ? payload.error.message
        : fallbackMessage;
    const requestId = isUuid(payload.error.request_id)
      ? payload.error.request_id
      : safeHeaderRequestId;
    return new ApiRequestError(message, {
      status: response.status,
      code,
      requestId,
    });
  }

  return new ApiRequestError(fallbackMessage, {
    status: response.status,
    requestId: safeHeaderRequestId,
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
    async deleteDocuments(
      input: DocumentDeletionInput,
      signal?: AbortSignal,
    ): Promise<DocumentDeletionsAccepted> {
      if (
        !isRecord(input) ||
        Object.keys(input).length !== 2 ||
        input.confirm_permanent !== true ||
        !Array.isArray(input.document_ids) ||
        input.document_ids.length === 0 ||
        input.document_ids.length > 100 ||
        !input.document_ids.every(isUuid) ||
        new Set(input.document_ids.map((id) => id.toLowerCase())).size !==
          input.document_ids.length
      ) {
        throw new ApiProtocolError(
          "Explicit confirmed document selection is required.",
        );
      }
      const accepted = await requestJson(
        fetchImplementation,
        "/api/v1/documents/deletions",
        isDocumentDeletionsAccepted,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(input),
          cache: "no-store",
          signal,
        },
      );
      const ids = input.document_ids.map((id) => id.toLowerCase());
      if (
        accepted.items.length !== ids.length ||
        accepted.items.some((item, index) => item.document_id !== ids[index]) ||
        new Set(accepted.dispatch_pending_document_ids).size !==
          accepted.dispatch_pending_document_ids.length ||
        accepted.dispatch_pending_document_ids.some(
          (id) =>
            !accepted.items.some(
              (item) => item.document_id === id && item.state === "PENDING",
            ),
        )
      ) {
        throw new ApiProtocolError();
      }
      return accepted;
    },

    async getDocumentDeletion(
      documentId: string,
      signal?: AbortSignal,
    ): Promise<DocumentDeletionResult> {
      if (!isUuid(documentId)) {
        throw new ApiProtocolError("The document identifier is invalid.");
      }
      const result = await requestJson(
        fetchImplementation,
        `/api/v1/documents/${documentId}/deletion`,
        isDocumentDeletionResult,
        { signal, cache: "no-store" },
      );
      if (result.document_id !== documentId.toLowerCase()) {
        throw new ApiProtocolError();
      }
      return result;
    },

    async listDocuments(
      after?: DocumentListCursor,
      signal?: AbortSignal,
    ): Promise<DocumentCatalogPage> {
      if (after !== undefined && !isDocumentListCursor(after)) {
        throw new ApiProtocolError("The document list cursor is invalid.");
      }
      const query = new URLSearchParams({ page_size: "20" });
      if (after !== undefined) {
        query.set("after_created_at", after.created_at);
        query.set("after_document_id", after.document_id);
      }
      const page = await requestJson(
        fetchImplementation,
        `/api/v1/documents?${query}`,
        isDocumentCatalogPage,
        { signal, cache: "no-store" },
      );
      if (
        page.items.length > 20 ||
        (after !== undefined &&
          page.next_cursor?.document_id === after.document_id &&
          page.next_cursor.created_at === after.created_at)
      ) {
        throw new ApiProtocolError("The document list did not advance.");
      }
      return page;
    },

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

    getResearchModelOptions(
      signal?: AbortSignal,
    ): Promise<ResearchModelOptions> {
      return requestJson(
        fetchImplementation,
        "/api/v1/research-requests/models",
        isResearchModelOptions,
        { signal },
      );
    },

    async *streamResearch(
      input: ResearchRequestInput,
      signal?: AbortSignal,
    ): AsyncGenerator<ResearchStreamEvent> {
      let response: Response;
      try {
        response = await fetchImplementation("/api/v1/research-requests", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(input),
          signal,
        });
      } catch (error) {
        if (signal?.aborted) {
          throw error;
        }
        throw new ApiTransportError(
          "The connection to the research API failed before a response arrived. The server may still finish; retrying can incur another provider charge.",
        );
      }
      if (!response.ok) {
        throw await requestError(
          response,
          `The web server did not receive a complete research response (HTTP ${response.status}). The API connection may have timed out. The server may still finish; retrying can incur another provider charge.`,
        );
      }

      try {
        yield* decodeResearchStream(response);
      } catch (error) {
        if (signal?.aborted || error instanceof ApiProtocolError) {
          throw error;
        }
        throw new ApiTransportError(
          "The connection to the research API closed before the answer finished. The server may still finish; retrying can incur another provider charge.",
        );
      }
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
        { signal, cache: "no-store" },
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
