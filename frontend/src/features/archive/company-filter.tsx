"use client";

import { useId, useState } from "react";

import {
  apiClient,
  type CompanyCatalogPage,
  type CompanySummary,
} from "@/lib/api/client";

const buttonClass =
  "rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs font-semibold text-slate-700 hover:border-teal-600 focus-visible:outline-2 focus-visible:outline-teal-700 disabled:opacity-50";

function companyLabel(company: CompanySummary): string {
  return company.ticker ? `${company.name} (${company.ticker})` : company.name;
}

export function CompanyFilter({
  company,
  disabled,
  onChange,
}: {
  readonly company: CompanySummary | null;
  readonly disabled: boolean;
  readonly onChange: (company: CompanySummary | null) => void;
}) {
  const searchId = useId();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<{
    query: string;
    page: CompanyCatalogPage;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  async function search(term: string, cursor?: string) {
    setLoading(true);
    setFailed(false);
    if (cursor === undefined) setResults(null);
    try {
      setResults({
        query: term,
        page: await apiClient.listCompanies(term, cursor),
      });
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }

  return (
    <fieldset disabled={disabled} className="space-y-2 text-sm text-slate-700">
      <legend className="text-xs font-semibold tracking-wide text-slate-500 uppercase">
        Company filter
      </legend>
      {company ? (
        <div className="flex flex-wrap items-center gap-2">
          <p>
            Company: <strong>{companyLabel(company)}</strong>
          </p>
          <button
            type="button"
            className={buttonClass}
            onClick={() => onChange(null)}
          >
            Clear company filter
          </button>
        </div>
      ) : (
        <>
          <form
            className="flex flex-wrap items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (!loading) void search(query.trim());
            }}
          >
            <label htmlFor={searchId} className="sr-only">
              Find a company by name or ticker
            </label>
            <input
              id={searchId}
              className="min-w-0 rounded-lg border border-slate-300 px-2.5 py-1.5 text-sm"
              placeholder="Company name or ticker"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <button type="submit" className={buttonClass} disabled={loading}>
              Find companies
            </button>
          </form>
          {loading && <p role="status">Finding companies…</p>}
          {failed && (
            <p role="alert">Companies could not be loaded. Try again.</p>
          )}
          {results && (
            <>
              {results.page.items.length === 0 && <p>No matching companies.</p>}
              <ul className="flex flex-wrap gap-2">
                {results.page.items.map((item) => (
                  <li key={item.company_id}>
                    <button
                      type="button"
                      className={buttonClass}
                      disabled={loading}
                      onClick={() => {
                        onChange(item);
                        setResults(null);
                      }}
                    >
                      {companyLabel(item)}
                    </button>
                  </li>
                ))}
              </ul>
              {results.page.next_cursor && (
                <button
                  type="button"
                  className={buttonClass}
                  disabled={loading}
                  onClick={() =>
                    void search(
                      results.query,
                      results.page.next_cursor ?? undefined,
                    )
                  }
                >
                  Next companies
                </button>
              )}
            </>
          )}
        </>
      )}
      <p className="text-xs text-slate-600">
        The company filter applies to the document list and to new questions.
      </p>
    </fieldset>
  );
}
