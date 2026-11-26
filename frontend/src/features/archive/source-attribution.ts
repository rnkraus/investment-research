export interface SourceAttribution {
  readonly author_name?: string | null;
  readonly username?: string | null;
}

export function readSourceAttribution(
  form: HTMLFormElement,
): Required<SourceAttribution> {
  const fields = new FormData(form);
  return {
    author_name: String(fields.get("author_name") ?? "").trim() || null,
    username: String(fields.get("username") ?? "").trim() || null,
  };
}

export function formatSourceAttribution(
  source: SourceAttribution,
): string | undefined {
  const author = source.author_name;
  const handle = source.username
    ? `@${source.username.replace(/^@/, "")}`
    : undefined;

  if (author && handle) {
    return `${author} (${handle})`;
  }
  return author ?? handle;
}
