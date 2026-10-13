import { archiveCopy } from "@/features/archive/copy";
import { ResearchWorkspace } from "@/features/archive/research-workspace";
import { productCopy } from "@/lib/product-copy";

export default function Home() {
  return (
    <main className="min-h-screen px-4 py-5 sm:px-6 lg:px-8">
      <div className="mx-auto max-w-7xl">
        <ResearchWorkspace />

        <footer className="mt-8 flex flex-col gap-2 border-t border-slate-200 py-5 text-xs text-slate-500 sm:flex-row sm:items-center sm:justify-between">
          <p>{productCopy.description}</p>
          <p>{archiveCopy.safetyNote}</p>
        </footer>
      </div>
    </main>
  );
}
