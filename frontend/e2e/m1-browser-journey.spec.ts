import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

const REPOSITORY_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const PDF_FIXTURE = path.join(
  REPOSITORY_ROOT,
  "fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf",
);
const TEXT_FIXTURE = path.join(
  REPOSITORY_ROOT,
  "fixtures/m1/documents/northstar-grid-analytics-fy2026-capacity-update.txt",
);
const PDF_QUESTION =
  "What was Northstar Grid Analytics' FY2025 revenue, and how did it change from FY2024?";
const PDF_STATEMENT =
  "Northstar Grid Analytics reported FY2025 revenue of $184.2 million, up 22.0% from $151.0 million in FY2024.";
const PDF_EVIDENCE =
  "FY2025 revenue was $184.2 million, up 22.0% from $151.0 million in FY2024.";
const TEXT_QUESTION =
  "What is mainly holding back Northstar's rollout to new clients?";
const TEXT_STATEMENT =
  "Northstar identified a shortage of high-voltage transformers as the primary constraint on new customer deployments.";
const TEXT_EVIDENCE =
  "The primary constraint on new customer deployments is a shortage of high-voltage transformers.";

async function waitUntilSearchable(page: Page) {
  await expect(page.getByText("SEARCHABLE", { exact: true })).toBeVisible({
    timeout: 90_000,
  });
  await expect(
    page.getByRole("heading", { name: "Ask the archive" }),
  ).toBeVisible();
}

async function askQuestion(page: Page, question: string, statement: string) {
  await page.getByLabel("Research question").fill(question);
  await page.getByRole("button", { name: "Research document" }).click();
  await expect(page.getByText(statement, { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText("Evidence coverage: COMPLETE")).toBeVisible();
}

async function openCitation(page: Page): Promise<Page> {
  const popupPromise = page.waitForEvent("popup");
  await page.getByRole("link", { name: "Open original source" }).click();
  const sourcePage = await popupPromise;
  await sourcePage.waitForLoadState("domcontentloaded");
  return sourcePage;
}

test.describe("M1 visible browser journey", () => {
  test("a PDF answer opens the immutable original at its physical page", async ({
    page,
  }) => {
    await page.goto("/");
    await page.getByLabel("PDF document").setInputFiles(PDF_FIXTURE);
    await page.getByRole("button", { name: "Import document" }).click();
    await waitUntilSearchable(page);

    await askQuestion(page, PDF_QUESTION, PDF_STATEMENT);
    await expect(page.getByText(PDF_EVIDENCE, { exact: true })).toBeVisible();

    const sourcePage = await openCitation(page);
    await expect(
      sourcePage.getByRole("heading", { name: "Original PDF" }),
    ).toBeVisible();
    await expect(sourcePage.locator("[data-pdf-page='2']")).toBeVisible();
    await expect(
      sourcePage.getByText(PDF_EVIDENCE, { exact: true }),
    ).toBeVisible();
    await expect(sourcePage.locator("iframe")).toHaveAttribute(
      "src",
      /#page=2$/,
    );
  });

  test("a pasted-text answer highlights its exact verified source range", async ({
    page,
  }) => {
    const sourceText = await readFile(TEXT_FIXTURE, "utf8");

    await page.goto("/");
    await page.getByText("Paste text", { exact: true }).click();
    await expect(page.getByLabel("Paste text")).toBeChecked();
    await page.getByLabel("Research text").fill(sourceText);
    await page
      .getByLabel("Document title")
      .fill("Northstar Grid Analytics Ltd. - FY2026 Capacity Update");
    await page.getByLabel("Publication date").fill("2026-07-22");
    await page.getByRole("button", { name: "Import document" }).click();
    await waitUntilSearchable(page);

    await askQuestion(page, TEXT_QUESTION, TEXT_STATEMENT);
    await expect(page.getByText(TEXT_EVIDENCE, { exact: true })).toBeVisible();

    const sourcePage = await openCitation(page);
    await expect(
      sourcePage.getByRole("heading", { name: "Original pasted text" }),
    ).toBeVisible();
    const highlight = sourcePage.getByTestId("source-highlight");
    await expect(highlight).toHaveText(TEXT_EVIDENCE);
    await expect(highlight).toHaveCount(1);
    await expect(sourcePage.locator("pre")).toHaveText(sourceText);
  });
});
