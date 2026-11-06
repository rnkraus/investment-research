import type { DocumentListFilters } from "@/lib/api/client";

import { archiveCopy } from "./copy";

const selectClass =
  "rounded-lg border border-slate-300 bg-white px-2 py-1.5 font-normal disabled:opacity-50";

export function DocumentListFilterControls({
  filters,
  disabled,
  onChange,
}: {
  readonly filters: DocumentListFilters;
  readonly disabled: boolean;
  readonly onChange: (filters: DocumentListFilters) => void;
}) {
  return (
    <fieldset
      disabled={disabled}
      className="mt-3 space-y-2 text-xs text-slate-700"
    >
      <legend className="sr-only">Document list filters</legend>
      <div className="grid grid-cols-2 gap-2">
        <label className="grid min-w-0 gap-1 font-semibold">
          Source format
          <select
            className={selectClass}
            value={filters.media_type ?? ""}
            onChange={(event) =>
              onChange({
                ...filters,
                media_type: (event.target.value ||
                  undefined) as DocumentListFilters["media_type"],
              })
            }
          >
            <option value="">All formats</option>
            <option value="application/pdf">PDF</option>
            <option value="text/plain">Pasted text</option>
          </select>
        </label>
        <label className="grid min-w-0 gap-1 font-semibold">
          Processing status
          <select
            className={selectClass}
            value={filters.processing_state ?? ""}
            onChange={(event) =>
              onChange({
                ...filters,
                processing_state: (event.target.value ||
                  undefined) as DocumentListFilters["processing_state"],
              })
            }
          >
            <option value="">All statuses</option>
            {Object.entries(archiveCopy.catalog.statuses).map(
              ([state, label]) => (
                <option key={state} value={state}>
                  {label}
                </option>
              ),
            )}
            <option value="NO_STATUS">
              {archiveCopy.catalog.notProcessed}
            </option>
          </select>
        </label>
      </div>
      <p>Filters this list. Select specific documents to narrow a question.</p>
    </fieldset>
  );
}
