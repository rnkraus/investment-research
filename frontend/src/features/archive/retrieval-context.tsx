import type {
  DocumentCatalogEntry,
  RetrievalContextSource,
} from "../../lib/api/client";
import { archiveCopy } from "./copy";

interface RetrievalContextProps {
  readonly sources: readonly RetrievalContextSource[];
  readonly documents: readonly DocumentCatalogEntry[];
}

export function RetrievalContext({
  sources,
  documents,
}: RetrievalContextProps) {
  const copy = archiveCopy.retrievalContext;
  const documentCount = new Set(sources.map((source) => source.document_id))
    .size;
  const characterCount = sources.reduce(
    (total, source) => total + Array.from(source.text).length,
    0,
  );

  return (
    <details
      className="mt-6 rounded-2xl border border-slate-200 bg-white p-5"
      data-testid="retrieval-context"
    >
      <summary className="cursor-pointer font-semibold text-slate-900 focus-visible:outline-2 focus-visible:outline-teal-700">
        {copy.heading} · {sources.length} {copy.chunks} · {documentCount}{" "}
        {copy.documents} · {characterCount.toLocaleString("en-US")}{" "}
        {copy.characters}
      </summary>
      <p className="mt-3 text-sm leading-6 text-slate-600">
        {copy.description}
      </p>
      {sources.length === 0 ? (
        <p className="mt-3 text-sm text-slate-700">{copy.empty}</p>
      ) : (
        <ol className="mt-4 space-y-3">
          {sources.map((source, index) => {
            const title = documents.find(
              (document) => document.document_id === source.document_id,
            )?.title;
            return (
              <li key={source.retrieval_candidate_id}>
                <details className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                  <summary className="cursor-pointer break-words text-sm font-semibold text-slate-900 focus-visible:outline-2 focus-visible:outline-teal-700">
                    {index + 1}.{" "}
                    {title ?? `${copy.document} ${source.document_id}`}
                    {" · "}
                    {source.source_locator_kind === "PDF_PAGE"
                      ? `${copy.page} ${source.page_number}`
                      : copy.textSource}
                  </summary>
                  <dl className="mt-3 grid gap-1 break-all text-xs text-slate-600">
                    <div>
                      <dt className="inline font-semibold">
                        {copy.document}:{" "}
                      </dt>
                      <dd className="inline">{source.document_id}</dd>
                    </div>
                    <div>
                      <dt className="inline font-semibold">{copy.version}: </dt>
                      <dd className="inline">{source.document_version_id}</dd>
                    </div>
                    <div>
                      <dt className="inline font-semibold">{copy.chunk}: </dt>
                      <dd className="inline">{source.chunk_id}</dd>
                    </div>
                  </dl>
                  <a
                    className="mt-3 inline-block text-sm font-semibold text-teal-800 underline underline-offset-4"
                    href={`/documents/${source.document_id}/versions/${source.document_version_id}`}
                    rel="noreferrer"
                    target="_blank"
                  >
                    {copy.openOriginal}
                  </a>
                  <p className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-slate-200 bg-white p-3 text-sm leading-6 text-slate-800">
                    {source.text}
                  </p>
                </details>
              </li>
            );
          })}
        </ol>
      )}
    </details>
  );
}
