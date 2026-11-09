export function formatSourceAttribution(source: {
  readonly author_name?: string | null;
  readonly x_handle?: string | null;
}): string | undefined {
  const author = source.author_name;
  const handle = source.x_handle
    ? `@${source.x_handle.replace(/^@/, "")}`
    : undefined;

  if (author && handle) {
    return `${author} (${handle})`;
  }
  return author ?? handle;
}
