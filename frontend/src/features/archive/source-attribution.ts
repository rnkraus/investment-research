export interface SourceAttribution {
  readonly author_name?: string | null;
  readonly x_handle?: string | null;
}

export function readSourceAttribution(
  form: HTMLFormElement,
): Required<SourceAttribution> {
  const fields = new FormData(form);
  return {
    author_name: String(fields.get("author_name") ?? "").trim() || null,
    x_handle: String(fields.get("x_handle") ?? "").trim() || null,
  };
}

export function formatSourceAttribution(
  source: SourceAttribution,
): string | undefined {
  const author = source.author_name;
  const handle = source.x_handle
    ? `@${source.x_handle.replace(/^@/, "")}`
    : undefined;

  if (author && handle) {
    return `${author} (${handle})`;
  }
  return author ?? handle;
}
