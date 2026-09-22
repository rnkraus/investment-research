import type { Metadata } from "next";

import { SourceViewer } from "@/features/archive/source-viewer";

export const metadata: Metadata = {
  title: "Verified source | Investment Research Archive",
};

interface SourcePageProps {
  readonly params: Promise<{
    readonly evidenceSpanId: string;
  }>;
}

export default async function SourcePage({ params }: SourcePageProps) {
  const { evidenceSpanId } = await params;
  return <SourceViewer evidenceSpanId={evidenceSpanId} />;
}
