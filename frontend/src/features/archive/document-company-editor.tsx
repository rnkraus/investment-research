"use client";

import { useEffect, useId, useState } from "react";

import {
  apiClient,
  ApiRequestError,
  COMPANY_ASSIGNMENT_LIMIT,
  type CompanyCatalogPage,
  type CompanySummary,
  type DocumentCompanyAssignment,
} from "@/lib/api/client";

const buttonClass =
  "rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-xs font-semibold text-slate-700 hover:border-teal-600 focus-visible:outline-2 focus-visible:outline-teal-700 disabled:opacity-50";
const inputClass =
  "w-full rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 focus-visible:outline-2 focus-visible:outline-teal-700";
// HTML counts UTF-16 units. Allow every 200-code-point name accepted by the API.
const nameInputMaxLength = 400;
const tickerMaxLength = 20;

function companyLabel(company: CompanySummary): string {
  return company.ticker ? `${company.name} (${company.ticker})` : company.name;
}

export function DocumentCompanyEditor({
  documentId,
  documentTitle,
  panelId,
}: {
  readonly documentId: string;
  readonly documentTitle: string;
  readonly panelId: string;
}) {
  const searchId = useId();
  const nameId = useId();
  const tickerId = useId();
  const [snapshot, setSnapshot] = useState<DocumentCompanyAssignment | null>(
    null,
  );
  const [selected, setSelected] = useState<readonly CompanySummary[]>([]);
  const [searchText, setSearchText] = useState("");
  const [results, setResults] = useState<{
    query: string;
    page: CompanyCatalogPage;
  } | null>(null);
  const [newName, setNewName] = useState("");
  const [newTicker, setNewTicker] = useState("");
  const [tickerEdit, setTickerEdit] = useState<{
    companyId: string;
    value: string;
  } | null>(null);
  const [pending, setPending] = useState<
    "load" | "search" | "create" | "save" | "ticker" | null
  >("load");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [needsReload, setNeedsReload] = useState(false);
  const [reloadCount, setReloadCount] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const current = await apiClient.getDocumentCompanies(
          documentId,
          controller.signal,
        );
        if (!controller.signal.aborted) {
          setSnapshot(current);
          setSelected(current.companies);
          setNeedsReload(false);
        }
      } catch {
        if (!controller.signal.aborted)
          setError("Could not load company assignments.");
      } finally {
        if (!controller.signal.aborted) setPending(null);
      }
    }
    void load();
    return () => controller.abort();
  }, [documentId, reloadCount]);

  const isBusy = pending !== null;
  const atLimit = selected.length >= COMPANY_ASSIGNMENT_LIMIT;
  const changed =
    snapshot !== null &&
    (snapshot.companies.length !== selected.length ||
      snapshot.companies.some(
        (company) =>
          !selected.some((item) => item.company_id === company.company_id),
      ));

  async function search(query: string, cursor?: string) {
    if (isBusy) return;
    setTickerEdit(null);
    setError(null);
    setNotice(null);
    setPending("search");
    try {
      setResults({ query, page: await apiClient.listCompanies(query, cursor) });
    } catch {
      setError(
        "Could not search companies. Run the search again to start from the first page.",
      );
    } finally {
      setPending(null);
    }
  }

  function selectCompany(company: CompanySummary) {
    setNotice(null);
    setSelected((current) =>
      current.length >= COMPANY_ASSIGNMENT_LIMIT ||
      current.some((item) => item.company_id === company.company_id)
        ? current
        : [...current, company],
    );
  }

  async function create() {
    const name = newName.trim();
    if (!name || isBusy || atLimit) return;
    setError(null);
    setNotice(null);
    setPending("create");
    try {
      selectCompany(
        await apiClient.createCompany(name, newTicker.trim() || undefined),
      );
      setNewName("");
      setNewTicker("");
      setNotice(
        "Company created and selected. Save companies to link it to this document.",
      );
    } catch (failure) {
      if (failure instanceof ApiRequestError && failure.status === 409) {
        setError("That company already exists. Search for it to select it.");
      } else if (failure instanceof ApiRequestError && failure.status === 422) {
        setError(
          "Enter a valid company name and a ticker of up to 20 letters, numbers, dots or hyphens.",
        );
      } else {
        setError(
          "Could not confirm company creation. Search for the name before retrying.",
        );
      }
    } finally {
      setPending(null);
    }
  }

  async function saveTicker() {
    if (tickerEdit === null || isBusy) return;
    setError(null);
    setNotice(null);
    setPending("ticker");
    try {
      const updated = await apiClient.updateCompanyTicker(
        tickerEdit.companyId,
        tickerEdit.value.trim() || null,
      );
      setSelected((current) =>
        current.map((company) =>
          company.company_id === updated.company_id ? updated : company,
        ),
      );
      setTickerEdit(null);
      // Restart the applied search because the edited company may no longer match its ticker.
      if (results !== null) {
        setResults(null);
        await search(results.query);
      }
      setNotice("Ticker saved.");
    } catch {
      setError(
        "Could not save the ticker. Search again to check the current ticker before retrying.",
      );
    } finally {
      setPending(null);
    }
  }

  async function save() {
    if (snapshot === null || isBusy || needsReload || !changed) return;
    setError(null);
    setNotice(null);
    setPending("save");
    try {
      const saved = await apiClient.replaceDocumentCompanies(documentId, {
        expected_document_version_id: snapshot.document_version_id,
        expected_revision: snapshot.revision,
        company_ids: selected.map((company) => company.company_id),
      });
      setSnapshot(saved);
      setSelected(saved.companies);
      setNotice("Company assignments saved.");
    } catch (failure) {
      setNeedsReload(true);
      setError(
        failure instanceof ApiRequestError && failure.status === 409
          ? "Company assignments changed elsewhere. Reload before saving again."
          : "Could not confirm your changes. Reload assignments before retrying.",
      );
    } finally {
      setPending(null);
    }
  }

  return (
    <section
      id={panelId}
      aria-label={`Companies for ${documentTitle}`}
      className="mt-3 space-y-3 rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs"
    >
      {pending === "load" && <p role="status">Loading company assignments…</p>}
      {error && (
        <p role="alert" className="text-rose-700">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="text-teal-800">
          {notice}
        </p>
      )}
      {(needsReload || snapshot === null) && pending !== "load" && (
        <button
          type="button"
          className={buttonClass}
          disabled={isBusy}
          onClick={() => {
            setError(null);
            setNotice(null);
            setPending("load");
            setReloadCount((current) => current + 1);
          }}
        >
          Reload assignments
        </button>
      )}
      {snapshot !== null && (
        <fieldset
          disabled={isBusy || needsReload}
          className="min-w-0 space-y-3"
        >
          <legend className="mb-2 font-semibold text-slate-900">
            Selected companies
          </legend>
          {selected.length === 0 ? (
            <p className="text-slate-600">No companies selected.</p>
          ) : (
            <ul className="space-y-1">
              {selected.map((company) => (
                <li
                  key={company.company_id}
                  className="flex items-center justify-between gap-2"
                >
                  <span className="min-w-0 break-words">
                    {companyLabel(company)}
                  </span>
                  <button
                    type="button"
                    className={buttonClass}
                    aria-label={`Remove ${company.name}`}
                    onClick={() => {
                      setNotice(null);
                      setSelected((current) =>
                        current.filter(
                          (item) => item.company_id !== company.company_id,
                        ),
                      );
                    }}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
          {atLimit && (
            <p className="text-slate-600">
              You can link up to {COMPANY_ASSIGNMENT_LIMIT} companies.
            </p>
          )}

          <form
            className="space-y-1.5"
            onSubmit={(event) => {
              event.preventDefault();
              void search(searchText.trim());
            }}
          >
            <label htmlFor={searchId} className="font-semibold">
              Search companies
            </label>
            <div className="flex gap-2">
              <input
                id={searchId}
                className={inputClass}
                maxLength={nameInputMaxLength}
                placeholder="Name or ticker"
                value={searchText}
                onChange={(event) => setSearchText(event.target.value)}
              />
              <button type="submit" className={buttonClass}>
                {pending === "search" ? "Searching…" : "Search"}
              </button>
            </div>
          </form>
          {results && (
            <div className="space-y-2" aria-label="Company search results">
              {results.page.items.length === 0 ? (
                <p>No matching companies.</p>
              ) : (
                <ul className="space-y-1">
                  {results.page.items.map((company) => {
                    const isSelected = selected.some(
                      (item) => item.company_id === company.company_id,
                    );
                    return (
                      <li key={company.company_id} className="space-y-1">
                        <div className="flex items-center justify-between gap-2">
                          <span className="min-w-0 break-words">
                            {companyLabel(company)}
                          </span>
                          <button
                            type="button"
                            className={buttonClass}
                            disabled={isSelected || atLimit}
                            aria-label={`${isSelected ? "Selected" : "Select"} ${company.name}`}
                            onClick={() => selectCompany(company)}
                          >
                            {isSelected ? "Selected" : "Select"}
                          </button>
                        </div>
                        {tickerEdit?.companyId === company.company_id ? (
                          <form
                            className="space-y-1"
                            onSubmit={(event) => {
                              event.preventDefault();
                              void saveTicker();
                            }}
                          >
                            <label
                              className="block font-semibold"
                              htmlFor={`${tickerId}-edit`}
                            >
                              Ticker for {company.name}
                            </label>
                            <input
                              id={`${tickerId}-edit`}
                              className={inputClass}
                              maxLength={tickerMaxLength}
                              value={tickerEdit.value}
                              onChange={(event) =>
                                setTickerEdit({
                                  companyId: company.company_id,
                                  value: event.target.value,
                                })
                              }
                            />
                            <div className="flex flex-wrap gap-2">
                              <button type="submit" className={buttonClass}>
                                Save ticker
                              </button>
                              <button
                                type="button"
                                className={buttonClass}
                                onClick={() => setTickerEdit(null)}
                              >
                                Cancel
                              </button>
                            </div>
                          </form>
                        ) : (
                          <button
                            type="button"
                            className="font-semibold text-teal-700 underline focus-visible:outline-2"
                            aria-label={`Edit ticker for ${company.name}`}
                            onClick={() =>
                              setTickerEdit({
                                companyId: company.company_id,
                                value: company.ticker ?? "",
                              })
                            }
                          >
                            Edit ticker
                          </button>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
              {results.page.next_cursor != null && (
                <button
                  type="button"
                  className={buttonClass}
                  onClick={() =>
                    void search(
                      results.query,
                      results.page.next_cursor ?? undefined,
                    )
                  }
                >
                  Next company page
                </button>
              )}
            </div>
          )}

          <form
            className="space-y-1.5"
            onSubmit={(event) => {
              event.preventDefault();
              void create();
            }}
          >
            <label htmlFor={nameId} className="font-semibold">
              New company name
            </label>
            <input
              id={nameId}
              className={inputClass}
              maxLength={nameInputMaxLength}
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
            />
            <label htmlFor={tickerId} className="block font-semibold">
              Ticker (optional)
            </label>
            <input
              id={tickerId}
              className={inputClass}
              maxLength={tickerMaxLength}
              placeholder="e.g. NVDA"
              value={newTicker}
              onChange={(event) => setNewTicker(event.target.value)}
            />
            <button
              type="submit"
              className={buttonClass}
              disabled={!newName.trim() || atLimit}
            >
              {pending === "create" ? "Creating…" : "Create and select"}
            </button>
          </form>

          <button
            type="button"
            className="rounded-lg bg-teal-700 px-3 py-2 font-semibold text-white hover:bg-teal-800 focus-visible:outline-2 focus-visible:outline-teal-700 disabled:opacity-50"
            disabled={!changed}
            onClick={() => void save()}
          >
            {pending === "save" ? "Saving…" : "Save companies"}
          </button>
        </fieldset>
      )}
    </section>
  );
}
