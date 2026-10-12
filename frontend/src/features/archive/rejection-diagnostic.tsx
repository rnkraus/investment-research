import type {
  DocumentCatalogEntry,
  ResearchRejectionDiagnostic,
  RetrievalContextSource,
} from "@/lib/api/client";

import { archiveCopy } from "./copy";

interface RejectionDiagnosticProps {
  readonly diagnostic: ResearchRejectionDiagnostic;
  readonly sources: readonly RetrievalContextSource[];
  readonly documents?: readonly DocumentCatalogEntry[];
}

export function RejectionDiagnostic({
  diagnostic,
  sources,
  documents = [],
}: RejectionDiagnosticProps) {
  const copy = archiveCopy.rejectionDiagnostic;
  return (
    <details
      open
      className="mt-4 rounded-xl border border-amber-300 bg-amber-50 p-4 text-slate-950"
    >
      <summary className="cursor-pointer font-semibold">{copy.heading}</summary>
      <p className="mt-3 text-sm leading-6">{copy.warning}</p>
      <p className="mt-2 text-xs">
        {copy.answerModel}: {diagnostic.answer_model} · {copy.supportModel}:{" "}
        {diagnostic.support_model}
      </p>
      {diagnostic.statements.length === 0 ? (
        <p className="mt-3 text-sm">{copy.empty}</p>
      ) : null}
      <ol className="mt-4 space-y-5">
        {diagnostic.statements.map((statement) => (
          <li
            key={statement.ordinal}
            className="rounded-lg border border-amber-200 bg-white p-4"
          >
            <h4 className="text-sm font-semibold">
              {copy.statement} {statement.ordinal + 1}
            </h4>
            <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6">
              {statement.text}
            </p>
            {statement.evidence.length === 0 ? (
              <p className="mt-3 text-sm">{copy.missingEvidence}</p>
            ) : null}
            {statement.evidence.map((range, index) => {
              const source = sources.find(
                (item) =>
                  item.retrieval_candidate_id === range.retrieval_candidate_id,
              );
              if (source === undefined) return null;
              const title =
                documents.find(
                  (item) => item.document_id === source.document_id,
                )?.title ?? source.document_id;
              const quote = Array.from(source.text)
                .slice(range.start_character, range.end_character)
                .join("");
              return (
                <div
                  key={`${range.retrieval_candidate_id}:${range.start_character}:${range.end_character}`}
                  className="mt-3"
                >
                  <p className="text-xs font-semibold">
                    {copy.evidence} {index + 1}: {title}
                    {source.page_number != null
                      ? ` · ${copy.page} ${source.page_number}`
                      : ""}
                  </p>
                  <blockquote className="mt-2 whitespace-pre-wrap break-words border-l-2 border-amber-400 pl-3 text-sm leading-6">
                    {quote}
                  </blockquote>
                </div>
              );
            })}
          </li>
        ))}
      </ol>
      <h4 className="mt-4 text-sm font-semibold">{copy.rules}</h4>
      <ul className="mt-2 space-y-2 text-sm">
        {diagnostic.findings.map((finding, index) => (
          <li key={index}>
            <span className="font-mono text-xs">{finding.rule}</span>
            {finding.statement_ordinal !== null
              ? ` · ${copy.statement} ${finding.statement_ordinal + 1}`
              : ""}
            : {finding.reason}
          </li>
        ))}
      </ul>
    </details>
  );
}
