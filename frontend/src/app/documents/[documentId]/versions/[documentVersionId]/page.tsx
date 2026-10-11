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
}

export default async function OriginalPage({ params }: OriginalPageProps) {
  const { documentId, documentVersionId } = await params;
  return (
    <OriginalViewer
      documentId={documentId}
      documentVersionId={documentVersionId}
    />
  );
}
