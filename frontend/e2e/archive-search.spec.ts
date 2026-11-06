import { readFile } from "node:fs/promises";

import { expect, test } from "@playwright/test";

const DOCUMENT = "10000000-0000-4000-8000-000000000001";
const VERSION = "10000000-0000-4000-8000-000000000002";
const CHUNK = "10000000-0000-4000-8000-000000000003";
const COMPANY = "10000000-0000-4000-8000-000000000004";
const ORIGINAL = `/documents/${DOCUMENT}/versions/${VERSION}`;
const PDF_FIXTURE = new URL(
  "../../fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf",
  import.meta.url,
);

test("archive result groups open originals and apply company filters without asking a question", async ({
  page,
  context,
}) => {
  let researchRequests = 0;
  const catalogCompanyIds: (string | null)[] = [];
  await context.route("**/api/v1/documents?*", (route) => {
    catalogCompanyIds.push(
      new URL(route.request().url()).searchParams.get("company_id"),
    );
    return route.fulfill({ json: { items: [], next_cursor: null } });
  });
  await context.route("**/api/v1/research-requests/models", (route) =>
    route.fulfill({
      json: {
        provider_enabled: false,
        generation_models: [],
        support_models: [],
      },
    }),
  );
  await context.route("**/api/v1/research-requests", (route) => {
    researchRequests += 1;
    return route.abort();
  });
  const searchBodies: unknown[] = [];
  await context.route("**/api/v1/archive-search", (route) => {
    expect(route.request().method()).toBe("POST");
    searchBodies.push(route.request().postDataJSON());
    return route.fulfill({
      json: {
        companies: [
          {
            company_id: COMPANY,
            name: "Northstar Grid",
            ticker: "NGRID",
            created_at: "2026-10-11T12:00:00Z",
          },
        ],
        documents: [
          {
            document_id: DOCUMENT,
            document_version_id: VERSION,
            title: "Northstar earnings brief",
            published_at: "2026-10-11T12:00:00Z",
          },
        ],
        items: [
          {
            chunk_id: CHUNK,
            document_id: DOCUMENT,
            document_version_id: VERSION,
            title: "Northstar earnings brief",
            published_at: "2026-10-11T12:00:00Z",
            source_locator_kind: "PDF_PAGE",
            page_number: 2,
            passage: "FY2025 revenue was $184.2 million.",
          },
        ],
        has_more: false,
      },
    });
  });
  await context.route(`**/api/v1${ORIGINAL}/original`, async (route) =>
    route.fulfill({
      body: await readFile(PDF_FIXTURE),
      contentType: "application/pdf",
    }),
  );

  await page.goto("/");
  await page.getByText("Search archive", { exact: true }).click();
  const input = page.getByLabel("Ticker, title, or keywords");
  const search = page.getByRole("button", { name: "Find matches" });
  await input.fill('"FY2025 revenue"');
  await search.click();
  const results = page.getByRole("region", { name: "Archive search results" });
  await expect(
    results.getByRole("heading", { name: "Companies", exact: true }),
  ).toBeVisible();
  await expect(
    results.getByRole("heading", { name: "Documents", exact: true }),
  ).toBeVisible();
  await expect(
    results.getByRole("heading", { name: "Text passages", exact: true }),
  ).toBeVisible();
  await expect(
    results.getByRole("link", { name: "Northstar earnings brief" }),
  ).toHaveAttribute("href", ORIGINAL);
  await results
    .getByRole("button", { name: "Use as company filter for Northstar Grid" })
    .click();
  await expect(
    page.getByRole("button", { name: "Clear company filter" }),
  ).toBeVisible();
  await expect.poll(() => catalogCompanyIds.at(-1)).toBe(COMPANY);
  await input.fill('"FY2025 revenue"');
  await search.click();
  await expect(
    results.getByRole("heading", { name: "Northstar earnings brief" }),
  ).toBeVisible();
  await expect(
    results.getByText("Page 2 · Published 2026-10-11"),
  ).toBeVisible();
  await expect(
    results.getByText("FY2025 revenue was $184.2 million."),
  ).toBeVisible();
  expect(searchBodies).toEqual([
    { query: '"FY2025 revenue"', mode: "keyword" },
    { company_id: COMPANY, query: '"FY2025 revenue"', mode: "keyword" },
  ]);
  await results.getByRole("link", { name: "Open original" }).click();
  await expect(page).toHaveURL(new RegExp(`${ORIGINAL}\\?page=2$`));
  await expect(page.locator("iframe")).toHaveAttribute(
    "src",
    /^blob:.*#page=2$/,
  );
  expect(researchRequests).toBe(0);
});

test("semantic archive search is opt-in and reports provider and budget failures", async ({
  page,
  context,
}) => {
  const query = "What slowed down expansion to new customers?";
  const searchModes: string[] = [];
  let semanticAttempts = 0;
  await context.route("**/api/v1/archive-search", (route) => {
    const body = route.request().postDataJSON();
    expect(body.query).toBe(query);
    searchModes.push(body.mode);
    if (body.mode === "keyword") {
      return route.fulfill({
        json: { companies: [], documents: [], items: [], has_more: false },
      });
    }
    semanticAttempts += 1;
    if (semanticAttempts <= 2) {
      return route.fulfill({
        status: semanticAttempts === 1 ? 429 : 503,
        json: {
          error: {
            code: "PROVIDER_UNAVAILABLE",
            message: "Synthetic provider failure",
          },
        },
      });
    }
    return route.fulfill({
      json: {
        companies: [],
        documents: [],
        items: [
          {
            chunk_id: CHUNK,
            document_id: DOCUMENT,
            document_version_id: VERSION,
            title: "Northstar earnings brief",
            published_at: null,
            source_locator_kind: "PDF_PAGE",
            page_number: 2,
            passage: "A shortage of transformers constrained deployments.",
          },
        ],
        has_more: false,
      },
    });
  });

  await page.goto("/");
  await page.getByText("Search archive", { exact: true }).click();
  const mode = page.getByLabel("Search mode");
  const submit = page.getByRole("button", { name: "Find matches" });
  await expect(mode).toHaveValue("keyword");
  await page.getByLabel("Ticker, title, or keywords").fill(query);
  await mode.selectOption("semantic");
  await expect(page.getByLabel("Describe the passage")).toHaveValue(query);
  await expect(page.getByText(/uses the workspace budget/)).toBeVisible();
  expect(searchModes).toEqual([]);

  await submit.click();
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "The workspace provider budget is exhausted." }),
  ).toBeVisible();
  await submit.click();
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "Semantic search is unavailable." }),
  ).toBeVisible();
  await submit.click();
  const results = page.getByRole("region", { name: "Archive search results" });
  await expect(
    results.getByRole("heading", { name: "Semantic passages" }),
  ).toBeVisible();
  await expect(
    results.getByText("A shortage of transformers constrained deployments."),
  ).toBeVisible();
  await expect(
    results.getByRole("link", { name: "Open original" }),
  ).toHaveAttribute("href", `${ORIGINAL}?page=2`);

  await mode.selectOption("keyword");
  await expect(results).toHaveCount(0);
  await expect(page.getByLabel("Ticker, title, or keywords")).toHaveValue(
    query,
  );
  await submit.click();
  await expect(results.getByRole("status")).toHaveText(
    `No matches for “${query}”.`,
  );
  expect(searchModes).toEqual(["semantic", "semantic", "semantic", "keyword"]);
});
