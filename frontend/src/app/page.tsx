import { productCopy } from "@/lib/product-copy";

const foundationItems = [
  "English-first interface",
  "FastAPI contract boundary",
  "Local PostgreSQL runtime",
] as const;

export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-5xl items-center px-6 py-20 sm:px-10">
      <section className="w-full overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-xl shadow-slate-200/60">
        <div className="bg-slate-900 px-8 py-10 text-white sm:px-12">
          <p className="text-sm font-semibold tracking-[0.2em] text-teal-300 uppercase">
            M0.0 local proof
          </p>
          <h1 className="mt-4 max-w-3xl text-4xl font-semibold tracking-tight sm:text-6xl">
            {productCopy.name}
          </h1>
          <p className="mt-5 max-w-2xl text-lg leading-8 text-slate-300">
            {productCopy.description}
          </p>
        </div>

        <div className="grid gap-8 px-8 py-10 sm:px-12 lg:grid-cols-[1.4fr_1fr]">
          <div>
            <h2 className="text-2xl font-semibold text-slate-900">
              Foundation in progress
            </h2>
            <p className="mt-3 max-w-xl leading-7 text-slate-600">
              This local-only application will import research documents,
              preserve their originals, and answer questions with verifiable
              citations.
            </p>
          </div>

          <ul className="space-y-3" aria-label="Foundation scope">
            {foundationItems.map((item) => (
              <li
                className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3 text-sm font-medium text-slate-700"
                key={item}
              >
                {item}
              </li>
            ))}
          </ul>
        </div>
      </section>
    </main>
  );
}
