import { useId } from "react";

import { archiveCopy } from "./copy";
import type { SourceAttribution } from "./source-attribution";

export function SourceAttributionFields({
  initial,
}: {
  readonly initial?: SourceAttribution;
}) {
  const inputId = useId();
  return (
    <>
      {[
        {
          name: "author_name",
          label: archiveCopy.upload.authorLabel,
          maxLength: 200,
          defaultValue: initial?.author_name,
        },
        {
          name: "x_handle",
          label: archiveCopy.upload.xHandleLabel,
          pattern: "@?[A-Za-z0-9_]{1,15}",
          title:
            "Use 1–15 letters, numbers, or underscores, with an optional @.",
          placeholder: archiveCopy.upload.xHandlePlaceholder,
          defaultValue: initial?.x_handle,
        },
      ].map(
        ({
          name,
          label,
          maxLength,
          pattern,
          placeholder,
          defaultValue,
          title,
        }) => {
          const id = `${inputId}-${name}`;
          return (
            <div key={name}>
              <label
                className="text-sm font-semibold text-slate-800"
                htmlFor={id}
              >
                {label}
              </label>
              <input
                className="mt-2 block w-full rounded-xl border border-slate-300 bg-white px-4 py-3 text-sm text-slate-950 shadow-xs outline-none placeholder:text-slate-400 focus:border-teal-600 focus:ring-3 focus:ring-teal-100"
                id={id}
                name={name}
                maxLength={maxLength}
                pattern={pattern}
                placeholder={placeholder}
                defaultValue={defaultValue ?? ""}
                title={title}
                type="text"
              />
            </div>
          );
        },
      )}
    </>
  );
}
