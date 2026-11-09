"use client";

import { useId, useState } from "react";

const buttonClass =
  "rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs font-semibold text-slate-700 hover:border-teal-600 focus-visible:outline-2 focus-visible:outline-teal-700 disabled:opacity-50";

interface ScopePage<Item> {
  readonly items: readonly Item[];
  readonly next_cursor?: string | null;
}

interface ArchiveScopePickerProps<Item> {
  readonly selected: Item | null;
  readonly disabled: boolean;
  readonly onChange: (item: Item | null) => void;
  readonly search: (query: string, cursor?: string) => Promise<ScopePage<Item>>;
  readonly itemKey: (item: Item) => string;
  readonly itemLabel: (item: Item) => string;
  readonly selectionLabel: (item: Item) => string;
  readonly title: string;
  readonly inputLabel: string;
  readonly placeholder: string;
  readonly maxLength?: number;
  readonly resultsName: string;
  readonly clearLabel: string;
}

export function ArchiveScopePicker<Item>({
  selected,
  disabled,
  onChange,
  search,
  itemKey,
  itemLabel,
  selectionLabel,
  title,
  inputLabel,
  placeholder,
  maxLength,
  resultsName,
  clearLabel,
}: ArchiveScopePickerProps<Item>) {
  const searchId = useId();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<{
    query: string;
    page: ScopePage<Item>;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  async function load(term: string, cursor?: string) {
    setLoading(true);
    setFailed(false);
    if (cursor === undefined) setResults(null);
    try {
      setResults({ query: term, page: await search(term, cursor) });
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }

  const nextCursor = results?.page.next_cursor;

  return (
    <fieldset disabled={disabled} className="space-y-2 text-sm text-slate-700">
      <legend className="text-xs font-semibold tracking-wide text-slate-500 uppercase">
        {title}
      </legend>
      {selected !== null ? (
        <div className="flex flex-wrap items-center gap-2">
          <p>
            {selectionLabel(selected)}: <strong>{itemLabel(selected)}</strong>
          </p>
          <button
            type="button"
            className={buttonClass}
            onClick={() => onChange(null)}
          >
            {clearLabel}
          </button>
        </div>
      ) : (
        <>
          <form
            className="flex flex-wrap items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (!loading) void load(query.trim());
            }}
          >
            <label htmlFor={searchId} className="sr-only">
              {inputLabel}
            </label>
            <input
              id={searchId}
              className="min-w-0 rounded-lg border border-slate-300 px-2.5 py-1.5 text-sm"
              type="search"
              maxLength={maxLength}
              placeholder={placeholder}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <button type="submit" className={buttonClass} disabled={loading}>
              Find {resultsName}
            </button>
          </form>
          {loading && <p role="status">Finding {resultsName}…</p>}
          {failed && (
            <p role="alert">Filter options could not be loaded. Try again.</p>
          )}
          {results && (
            <>
              {results.page.items.length === 0 ? (
                <p>No matching {resultsName}.</p>
              ) : (
                <ul className="flex flex-wrap gap-2">
                  {results.page.items.map((item) => (
                    <li key={itemKey(item)}>
                      <button
                        type="button"
                        className={buttonClass}
                        disabled={loading}
                        onClick={() => {
                          setQuery("");
                          setResults(null);
                          onChange(item);
                        }}
                      >
                        {itemLabel(item)}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {nextCursor && (
                <button
                  type="button"
                  className={buttonClass}
                  disabled={loading}
                  onClick={() => void load(results.query, nextCursor)}
                >
                  Next {resultsName}
                </button>
              )}
            </>
          )}
        </>
      )}
      <p className="text-xs text-slate-600">
        Applies to the document list, archive searches, and new questions.
      </p>
    </fieldset>
  );
}
