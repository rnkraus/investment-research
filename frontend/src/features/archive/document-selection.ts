import type { DocumentCatalogEntry } from "../../lib/api/client";

export const DOCUMENT_SELECTION_LIMIT = 100;

export interface DocumentSelection {
  readonly scope: "ALL" | "SELECTED";
  readonly documents: readonly DocumentCatalogEntry[];
}

export const EMPTY_DOCUMENT_SELECTION: DocumentSelection = {
  scope: "ALL",
  documents: [],
};

export function isDocumentSelectable(document: DocumentCatalogEntry): boolean {
  return (
    document.processing === null ||
    document.processing.state === "CAPTURED" ||
    document.processing.state === "LEXICAL_READY" ||
    document.processing.state === "SEARCHABLE" ||
    document.processing.state === "FAILED"
  );
}

export function toggleDocumentSelection(
  selection: DocumentSelection,
  document: DocumentCatalogEntry,
): DocumentSelection {
  if (
    selection.documents.some(
      (item) => item.document_id === document.document_id,
    )
  ) {
    return {
      scope: "SELECTED",
      documents: selection.documents.filter(
        (item) => item.document_id !== document.document_id,
      ),
    };
  }
  if (
    !isDocumentSelectable(document) ||
    selection.documents.length >= DOCUMENT_SELECTION_LIMIT
  ) {
    return selection;
  }
  return { scope: "SELECTED", documents: [...selection.documents, document] };
}

export function getDeletionDocumentIds(
  selection: DocumentSelection,
): readonly string[] | undefined {
  if (
    selection.scope !== "SELECTED" ||
    selection.documents.length === 0 ||
    selection.documents.length > DOCUMENT_SELECTION_LIMIT ||
    selection.documents.some((item) => !isDocumentSelectable(item))
  ) {
    return undefined;
  }
  const ids = selection.documents.map((item) => item.document_id);
  return new Set(ids).size === ids.length ? ids : undefined;
}

export function resetDocumentSelection(
  selection: DocumentSelection,
): DocumentSelection {
  // Preserve explicit scope: an empty selection must never become an archive-wide query.
  return { scope: selection.scope, documents: [] };
}

export function getResearchDocumentIds(
  selection: DocumentSelection,
): readonly string[] | undefined {
  if (selection.scope === "ALL") {
    return [];
  }
  if (
    selection.documents.length === 0 ||
    selection.documents.length > DOCUMENT_SELECTION_LIMIT ||
    selection.documents.some((item) => item.processing?.state !== "SEARCHABLE")
  ) {
    return undefined;
  }
  const ids = selection.documents.map((item) => item.document_id);
  return new Set(ids).size === ids.length ? ids : undefined;
}
