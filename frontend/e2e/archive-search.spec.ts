import { readFile } from "node:fs/promises";

import { expect, test } from "@playwright/test";

const DOCUMENT = "10000000-0000-4000-8000-000000000001";
const VERSION = "10000000-0000-4000-8000-000000000002";
const CHUNK = "10000000-0000-4000-8000-000000000003";
const ORIGINAL = `/documents/${DOCUMENT}/versions/${VERSION}`;
const PDF_FIXTURE = new URL(
  "../../fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf",
  import.meta.url,
);

test("a passage search opens the matching original PDF page without asking a question", async ({
  page,
  context,
}) => {
  let researchRequests = 0;
  await context.route("**/api/v1/documents?*", (route) =>
    route.fulfill({ json: { items: [], next_cursor: null } }),
  );
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
  await context.route("**/api/v1/archive-search", (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({
      query: '"FY2025 revenue"',
    });
    return route.fulfill({
      json: {
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
  await page.getByLabel("Keywords or phrase").fill('"FY2025 revenue"');
  await page.getByRole("button", { name: "Search passages" }).click();
  const results = page.getByRole("region", { name: "Archive search results" });
  await expect(
    results.getByRole("heading", { name: "Northstar earnings brief" }),
  ).toBeVisible();
  await expect(
    results.getByText("Page 2 · Published 2026-10-11"),
  ).toBeVisible();
  await expect(
    results.getByText("FY2025 revenue was $184.2 million."),
  ).toBeVisible();
  await results.getByRole("link", { name: "Open original" }).click();
  await expect(page).toHaveURL(new RegExp(`${ORIGINAL}\\?page=2$`));
  await expect(page.locator("iframe")).toHaveAttribute(
    "src",
    /^blob:.*#page=2$/,
  );
  expect(researchRequests).toBe(0);
});
