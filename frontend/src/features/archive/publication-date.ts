/** Format a date input value using the user's local calendar, not the UTC day. */
export function getLocalPublicationDate(now: Date = new Date()): string {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** Preserve the uploaded publication day stored as a canonical UTC timestamp. */
export function formatPublicationDate(
  value: string | null | undefined,
): string | undefined {
  return value == null ? undefined : new Date(value).toISOString().slice(0, 10);
}
