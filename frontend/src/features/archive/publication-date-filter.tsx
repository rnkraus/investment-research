"use client";

import { useId, useState } from "react";

import type { ArchiveFilters } from "@/lib/api/client";

export type PublicationDates = Pick<
  ArchiveFilters,
  "published_from" | "published_to"
>;

export function PublicationDateFilter({
  applied,
  disabled,
  onApply,
}: {
  readonly applied: PublicationDates;
  readonly disabled: boolean;
  readonly onApply: (dates: PublicationDates) => void;
}) {
  const fromId = useId();
  const toId = useId();
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const reversed = from !== "" && to !== "" && from > to;
  const active = applied.published_from != null || applied.published_to != null;
  const inputClass = "rounded-lg border border-slate-300 px-2 py-1.5";
  const buttonClass =
    "rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs font-semibold hover:border-teal-600 focus-visible:outline-2 focus-visible:outline-teal-700 disabled:opacity-50";

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (!reversed) {
          onApply({
            published_from: from || undefined,
            published_to: to || undefined,
          });
        }
      }}
    >
      <fieldset
        disabled={disabled}
        className="space-y-2 text-sm text-slate-700"
      >
        <legend className="text-xs font-semibold tracking-wide text-slate-500 uppercase">
          Publication date
        </legend>
        <div className="flex flex-wrap items-end gap-2">
          <label htmlFor={fromId} className="grid gap-1">
            Published from
            <input
              id={fromId}
              type="date"
              min="0001-01-01"
              max="9999-12-31"
              value={from}
              onChange={(event) => setFrom(event.target.value)}
              className={inputClass}
            />
          </label>
          <label htmlFor={toId} className="grid gap-1">
            Published through
            <input
              id={toId}
              type="date"
              min="0001-01-01"
              max="9999-12-31"
              value={to}
              onChange={(event) => setTo(event.target.value)}
              className={inputClass}
            />
          </label>
          <button type="submit" disabled={reversed} className={buttonClass}>
            Apply dates
          </button>
          {active && (
            <button
              type="button"
              className={buttonClass}
              onClick={() => {
                setFrom("");
                setTo("");
                onApply({});
              }}
            >
              Clear dates
            </button>
          )}
        </div>
        {reversed && (
          <p role="alert">The start date must be on or before the end date.</p>
        )}
        <p aria-live="polite" className="text-xs text-slate-600">
          {active
            ? `Applied: ${applied.published_from ?? "Any start"} through ${applied.published_to ?? "any end"} (UTC, inclusive).`
            : "Applies to the document list and new questions."}
        </p>
      </fieldset>
    </form>
  );
}
