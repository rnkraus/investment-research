"use client";

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";

import {
  apiClient,
  type ArchiveFilters,
  type ArchiveSearchResult,
  type CompanySummary,
} from "@/lib/api/client";

import { formatPublicationDate } from "./publication-date";

type SearchState =
  | { readonly status: "LOADING" | "ERROR" }
  | {
      readonly status: "RESULTS";
      readonly query: string;
      readonly result: ArchiveSearchResult;
    };

export function ArchivePassageSearch({
  filters,
  disabled,
  onSelectCompany,
}: {
  readonly filters: ArchiveFilters;
  readonly disabled: boolean;
  readonly onSelectCompany: (company: CompanySummary) => void;
}) {
  const queryId = useId();
  const request = useRef<AbortController | null>(null);
  const [query, setQuery] = useState("");
  const [state, setState] = useState<SearchState>();
  const loading = state?.status === "LOADING";
  const result = state?.status === "RESULTS" ? state.result : undefined;

  useEffect(() => () => request.current?.abort(), []);

  async function search() {
    const term = query.trim();
    if (!term || loading || disabled) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setState({ status: "LOADING" });
    try {
      const result = await apiClient.searchArchive(
        { ...filters, query: term },
        controller.signal,
      );
      if (!controller.signal.aborted)
        setState({ status: "RESULTS", query: term, result });
    } catch {
      if (!controller.signal.aborted) setState({ status: "ERROR" });
    }
  }

  return (
    <div className="mt-3 space-y-3">
      <p className="text-sm text-slate-600">
        Search by ticker, document title, or keywords. Use quotation marks to
        find a phrase in the text. The selected company and publication dates
        narrow document and passage matches.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void search();
        }}
      >
        <label
          htmlFor={queryId}
          className="text-sm font-semibold text-slate-700"
        >
          Ticker, title, or keywords
        </label>
        <div className="mt-1 flex flex-wrap gap-2">
          <input
            id={queryId}
            type="search"
            value={query}
            maxLength={300}
            disabled={disabled}
            onChange={(event) => setQuery(event.target.value)}
            className="min-w-40 flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm"
          />
          <button
            type="submit"
            disabled={disabled || loading || !query.trim()}
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-semibold text-teal-800 hover:border-teal-600 focus-visible:outline-2 focus-visible:outline-teal-700 disabled:opacity-50"
          >
            Find matches
          </button>
        </div>
      </form>
      {loading && (
        <p role="status" className="text-sm">
          Searching the archive…
        </p>
      )}
      {state?.status === "ERROR" && (
        <p role="alert" className="text-sm text-rose-700">
          Archive search failed. Try again.
        </p>
      )}
      {state?.status === "RESULTS" && result && (
        <section aria-label="Archive search results" className="space-y-3">
          <p role="status" className="text-sm text-slate-600">
            {result.companies.length ||
            result.documents.length ||
            result.items.length
              ? `Matches for “${state.query}”.`
              : `No matches for “${state.query}”.`}
          </p>
          {result.companies.length > 0 && (
            <div className="space-y-2">
              <h3 className="text-sm font-semibold text-slate-900">
                Companies
              </h3>
              <ul className="space-y-2">
                {result.companies.map((company) => (
                  <li
                    key={company.company_id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-200 p-3 text-sm"
                  >
                    <span>
                      {company.name}
                      {company.ticker ? ` (${company.ticker})` : ""}
                    </span>
                    <button
                      type="button"
                      disabled={
                        disabled || filters.company_id === company.company_id
                      }
                      onClick={() => onSelectCompany(company)}
                      aria-label={
                        filters.company_id === company.company_id
                          ? undefined
                          : `Use as company filter for ${company.name}`
                      }
                      className="font-semibold text-teal-800 underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-teal-700 disabled:opacity-50"
                    >
                      {filters.company_id === company.company_id
                        ? "Company selected"
                        : "Use as company filter"}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {result.documents.length > 0 && (
            <div className="space-y-2">
              <h3 className="text-sm font-semibold text-slate-900">
                Documents
              </h3>
              <ul className="space-y-2">
                {result.documents.map((document) => {
                  const date = formatPublicationDate(document.published_at);
                  return (
                    <li
                      key={document.document_id}
                      className="rounded-xl border border-slate-200 p-3"
                    >
                      <Link
                        href={`/documents/${document.document_id}/versions/${document.document_version_id}`}
                        className="text-sm font-semibold text-teal-800 underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-teal-700"
                      >
                        {document.title}
                      </Link>
                      {date && (
                        <p className="mt-1 text-xs text-slate-600">
                          Published {date}
                        </p>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
          {result.items.length > 0 && (
            <div className="space-y-2">
              <h3 className="text-sm font-semibold text-slate-900">
                Text passages
              </h3>
              {result.has_more && (
                <p className="text-xs text-slate-600">
                  Refine the search to narrow the passage matches.
                </p>
              )}
              <ol className="space-y-3">
                {result.items.map((hit) => {
                  const original = `/documents/${hit.document_id}/versions/${hit.document_version_id}`;
                  const page = hit.page_number;
                  const location =
                    page === null ? "Pasted text" : `Page ${page}`;
                  const date = formatPublicationDate(hit.published_at);
                  return (
                    <li
                      key={hit.chunk_id}
                      className="rounded-xl border border-slate-200 p-3"
                    >
                      <h4 className="text-sm font-semibold text-slate-950">
                        {hit.title}
                      </h4>
                      <p className="mt-1 text-xs text-slate-600">
                        {location}
                        {date ? ` · Published ${date}` : ""}
                      </p>
                      <p className="mt-2 text-sm whitespace-pre-wrap [overflow-wrap:anywhere]">
                        {hit.passage}
                      </p>
                      <Link
                        href={
                          page === null ? original : `${original}?page=${page}`
                        }
                        className="mt-2 inline-block text-sm font-semibold text-teal-800 underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-teal-700"
                      >
                        Open original
                      </Link>
                    </li>
                  );
                })}
              </ol>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
