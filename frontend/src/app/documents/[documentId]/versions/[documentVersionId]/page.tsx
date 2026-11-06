import type { Metadata } from "next";

import { OriginalViewer } from "@/features/archive/original-viewer";

export const metadata: Metadata = {
  title: "Original document | Investment Research Archive",
};

interface OriginalPageProps {
  readonly params: Promise<{
    readonly documentId: string;
    readonly documentVersionId: string;
  }>;
  readonly searchParams: Promise<{ readonly page?: string | string[] }>;
}

export default async function OriginalPage({
  params,
  searchParams,
}: OriginalPageProps) {
  const [{ documentId, documentVersionId }, { page }] = await Promise.all([
    params,
    searchParams,
  ]);
  const pageNumber =
    typeof page === "string" && /^[1-9]\d*$/.test(page)
      ? Number(page)
      : undefined;
  return (
    <OriginalViewer
      documentId={documentId}
      documentVersionId={documentVersionId}
      page={Number.isSafeInteger(pageNumber) ? pageNumber : undefined}
    />
  );
}
