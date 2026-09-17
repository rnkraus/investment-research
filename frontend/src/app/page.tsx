import { archiveCopy } from "@/features/archive/copy";
import { DocumentWorkflow } from "@/features/archive/document-workflow";
import { productCopy } from "@/lib/product-copy";

export default function Home() {
  return (
    <main className="min-h-screen px-5 py-6 sm:px-8 lg:px-12">
      <div className="mx-auto max-w-7xl">
        <header className="flex items-center justify-between border-b border-slate-200 pb-5">
          <div className="flex items-center gap-3">
            <span
              aria-hidden="true"
              className="grid size-10 place-items-center rounded-xl bg-slate-950 text-sm font-bold tracking-tight text-white"
            >
              IR
            </span>
            <div>
              <p className="font-semibold tracking-tight text-slate-950">
                {productCopy.name}
              </p>
              <p className="text-xs text-slate-500">
                {archiveCopy.workspaceLabel}
              </p>
            </div>
          </div>
          <p className="hidden rounded-full border border-teal-200 bg-teal-50 px-3 py-1.5 text-xs font-semibold text-teal-800 sm:block">
            {archiveCopy.localOnly}
          </p>
        </header>

        <section className="grid gap-10 py-14 lg:grid-cols-[1.2fr_0.8fr] lg:items-end lg:py-20">
          <div>
            <p className="text-xs font-bold tracking-[0.22em] text-teal-700 uppercase">
              {archiveCopy.eyebrow}
            </p>
            <h1 className="mt-4 max-w-4xl text-4xl font-semibold tracking-[-0.04em] text-balance text-slate-950 sm:text-6xl lg:text-7xl">
              {archiveCopy.heading}
            </h1>
            <p className="mt-6 max-w-2xl text-lg leading-8 text-slate-600">
              {archiveCopy.introduction}
            </p>
          </div>

          <ol
            aria-label="M1 workflow"
            className="grid grid-cols-2 gap-px overflow-hidden rounded-2xl border border-slate-200 bg-slate-200"
          >
            {archiveCopy.steps.map((step, index) => (
              <li className="bg-white p-4 sm:p-5" key={step}>
                <span className="text-xs font-bold text-teal-700">
                  0{index + 1}
                </span>
                <p className="mt-1 text-sm font-semibold text-slate-800">
                  {step}
                </p>
              </li>
            ))}
          </ol>
        </section>

        <DocumentWorkflow />

        <footer className="mt-12 flex flex-col gap-2 border-t border-slate-200 py-6 text-xs text-slate-500 sm:flex-row sm:items-center sm:justify-between">
          <p>{productCopy.description}</p>
          <p>{archiveCopy.safetyNote}</p>
        </footer>
      </div>
    </main>
  );
}
