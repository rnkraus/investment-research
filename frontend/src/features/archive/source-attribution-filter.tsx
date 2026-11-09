import { apiClient, type SourceAttributionChoice } from "@/lib/api/client";

import { ArchiveScopePicker } from "./archive-scope-picker";

export function SourceAttributionFilter({
  selected,
  disabled,
  onChange,
}: {
  readonly selected: SourceAttributionChoice | null;
  readonly disabled: boolean;
  readonly onChange: (choice: SourceAttributionChoice | null) => void;
}) {
  return (
    <ArchiveScopePicker
      selected={selected}
      disabled={disabled}
      onChange={onChange}
      search={apiClient.findSourceAttributions}
      itemKey={(item) => `${item.kind}:${item.value}`}
      itemLabel={(item) => item.value}
      selectionLabel={(item) =>
        item.kind === "handle" ? "X handle" : "Author"
      }
      title="Source attribution filter"
      inputLabel="Author or X handle"
      placeholder="Name or @handle"
      maxLength={200}
      resultsName="authors or handles"
      clearLabel="Clear attribution filter"
    />
  );
}
