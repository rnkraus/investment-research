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
const PDF_TITLE = "M1 browser PDF source";
const TEXT_TITLE = "M1 browser pasted-text source";

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
  await page.getByRole("button", { name: "Ask the archive" }).click();
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
    await page.getByLabel("Document title").fill(PDF_TITLE);
    await page.getByRole("button", { name: "Import document" }).click();
    await waitUntilSearchable(page);

    await page
      .getByRole("checkbox", {
        name: `Select document: ${PDF_TITLE}`,
        exact: true,
      })
      .check();
    await askQuestion(page, PDF_QUESTION, PDF_STATEMENT);
    await expect(page.getByText(PDF_EVIDENCE, { exact: true })).toBeVisible();

    const retrievedContext = page.getByTestId("retrieval-context");
    await expect(retrievedContext).not.toHaveAttribute("open", "");
    await retrievedContext.locator(":scope > summary").click();
    await expect(retrievedContext).toHaveAttribute("open", "");
    await expect(retrievedContext).toContainText(PDF_EVIDENCE);
    const firstPassage = retrievedContext.locator("ol > li > details").first();
    await firstPassage.locator("summary").click();
    await expect(firstPassage.locator("summary")).toContainText(PDF_TITLE);
    await expect(firstPassage.locator("p")).toBeVisible();
    await expect(
      firstPassage.getByRole("link", { name: "Open original", exact: true }),
    ).toHaveAttribute(
      "href",
      /^\/documents\/[a-f0-9-]+\/versions\/[a-f0-9-]+$/,
    );
    await retrievedContext.locator(":scope > summary").click();
    await expect(firstPassage.locator("p")).not.toBeVisible();

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

    await page.reload();
    await expect(
      page.getByRole("heading", { name: "Ask the archive" }),
    ).toBeVisible();
    await expect(page.getByLabel("All saved documents")).toBeChecked();
    await expect(
      page.getByRole("checkbox", {
        name: `Select document: ${PDF_TITLE}`,
        exact: true,
      }),
    ).toBeEnabled();
    const originalPopup = page.waitForEvent("popup");
    await page
      .getByRole("link", { name: `Open original: ${PDF_TITLE}`, exact: true })
      .click();
    const originalPage = await originalPopup;
    await expect(
      originalPage.getByRole("heading", { name: "Original PDF" }),
    ).toBeVisible();
    await expect(originalPage.locator("iframe")).toHaveAttribute(
      "src",
      /^blob:/,
    );
    await originalPage.close();
    await page
      .getByRole("checkbox", {
        name: `Select document: ${PDF_TITLE}`,
        exact: true,
      })
      .check();
    await askQuestion(page, PDF_QUESTION, PDF_STATEMENT);
    await expect(page.getByText(PDF_EVIDENCE, { exact: true })).toBeVisible();
  });

  test("a pasted-text answer highlights its exact verified source range", async ({
    page,
  }) => {
    const sourceText = await readFile(TEXT_FIXTURE, "utf8");

    await page.goto("/");
    await page.getByText("Paste text", { exact: true }).click();
    await expect(page.getByLabel("Paste text")).toBeChecked();
    await page.getByLabel("Research text").fill(sourceText);
    await page.getByLabel("Document title").fill(TEXT_TITLE);
    await page.getByLabel("Publication date").fill("2026-07-22");
    await page.getByRole("button", { name: "Import document" }).click();
    await waitUntilSearchable(page);

    await page
      .getByRole("checkbox", {
        name: `Select document: ${TEXT_TITLE}`,
        exact: true,
      })
      .check();
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

  test("saved-source selection survives hiding and pagination and clears safely", async ({
    page,
    context,
  }) => {
    const versionId = "10000000-0000-4000-8000-000000000099";
    const makeEntry = (ordinal: number, state: "SEARCHABLE" | "PARSING") => {
      const id = `10000000-0000-4000-8000-${String(ordinal).padStart(12, "0")}`;
      return {
        document_id: id,
        document_version_id: versionId,
        title: `Catalog source ${ordinal}`,
        language: "en",
        media_type: "text/plain",
        byte_size: 32,
        created_at: `2026-10-${String(20 - ordinal).padStart(2, "0")}T12:00:00Z`,
        captured_at: "2026-10-11T12:00:00Z",
        published_at: null,
        available_at: null,
        processing: {
          processing_run_id: id,
          document_version_id: versionId,
          state,
          error_summary: null,
        },
        original_url: `/api/v1/documents/${id}/versions/${versionId}/original`,
      };
    };
    const first = makeEntry(1, "SEARCHABLE");
    const pending = makeEntry(2, "PARSING");
    const second = makeEntry(3, "SEARCHABLE");
    let submittedIds: readonly string[] | undefined;
    await context.route("**/api/v1/research-requests/models", (route) =>
      route.fulfill({
        json: {
          provider_enabled: false,
          generation_models: [],
          support_models: [],
        },
      }),
    );
    await context.route("**/api/v1/documents?*", (route) => {
      const nextPage = new URL(route.request().url()).searchParams.has(
        "after_document_id",
      );
      return route.fulfill({
        json: nextPage
          ? { items: [second], next_cursor: null }
          : {
              items: [first, pending],
              next_cursor: {
                created_at: pending.created_at,
                document_id: pending.document_id,
              },
            },
      });
    });
    await context.route("**/api/v1/research-requests", (route) => {
      submittedIds = route.request().postDataJSON().context.document_ids;
      return route.fulfill({
        contentType: "text/event-stream",
        body: `event: answer.insufficient\ndata: ${JSON.stringify({
          event: "answer.insufficient",
          schema_version: "1.0",
          research_request_id: versionId,
          coverage: "INSUFFICIENT",
          message: "No supported answer in these test sources.",
        })}\n\n`,
      });
    });
    await context.route(`**${first.original_url}`, (route) =>
      route.fulfill({
        contentType: "text/plain; charset=utf-8",
        body: "Saved text: <script>not executable</script> 🧪\n",
      }),
    );
    await page.goto("/");
    await expect(
      page.getByRole("checkbox", {
        name: "Select document: Catalog source 1",
        exact: true,
      }),
    ).toBeEnabled();
    await expect(
      page.getByRole("checkbox", {
        name: "Select document: Catalog source 2",
        exact: true,
      }),
    ).toBeDisabled();
    await page
      .getByRole("radio", { name: "Selected documents", exact: true })
      .check();
    const submit = page.getByRole("button", {
      name: "Ask the archive",
      exact: true,
    });
    await expect(submit).toBeDisabled();
    await page
      .getByRole("checkbox", {
        name: "Select document: Catalog source 1",
        exact: true,
      })
      .check();
    await page.getByRole("button", { name: "Load more documents" }).click();
    await page
      .getByRole("checkbox", {
        name: "Select document: Catalog source 3",
        exact: true,
      })
      .check();
    await expect(
      page.getByRole("checkbox", {
        name: "Select document: Catalog source 1",
        exact: true,
      }),
    ).toBeChecked();
    await expect(
      page.getByText("Selected documents: 2.", { exact: true }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Hide documents", exact: true })
      .click();
    const showDocuments = page.getByRole("button", {
      name: "Show documents",
      exact: true,
    });
    await expect(showDocuments).toHaveAttribute("aria-expanded", "false");
    await expect(
      page.getByRole("checkbox", {
        name: "Select document: Catalog source 1",
        exact: true,
      }),
    ).toBeHidden();
    await expect(
      page.getByText("Selected documents: 2.", { exact: true }),
    ).toBeVisible();
    await expect(submit).toBeEnabled();
    await submit.click();
    await expect(
      page.getByRole("heading", { name: "Insufficient evidence" }),
    ).toBeVisible();
    expect(submittedIds).toEqual([first.document_id, second.document_id]);

    await showDocuments.click();
    await expect(
      page.getByRole("button", { name: "Hide documents", exact: true }),
    ).toHaveAttribute("aria-expanded", "true");
    await expect(
      page.getByRole("checkbox", {
        name: "Select document: Catalog source 1",
        exact: true,
      }),
    ).toBeChecked();
    await expect(
      page.getByRole("checkbox", {
        name: "Select document: Catalog source 3",
        exact: true,
      }),
    ).toBeChecked();
    await page
      .getByRole("button", { name: "Clear selection", exact: true })
      .click();
    await expect(
      page.getByRole("radio", { name: "Selected documents", exact: true }),
    ).toBeChecked();
    await expect(
      page.getByRole("checkbox", {
        name: "Select document: Catalog source 1",
        exact: true,
      }),
    ).not.toBeChecked();
    await expect(
      page.getByRole("checkbox", {
        name: "Select document: Catalog source 3",
        exact: true,
      }),
    ).not.toBeChecked();
    await expect(submit).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "Clear selection", exact: true }),
    ).toBeDisabled();
    await page.getByText("Catalog source 1", { exact: true }).click();
    await expect(
      page.getByRole("checkbox", {
        name: "Select document: Catalog source 1",
        exact: true,
      }),
    ).toBeChecked();

    const popupPromise = page.waitForEvent("popup");
    await page
      .getByRole("link", {
        name: "Open original: Catalog source 1",
        exact: true,
      })
      .click();
    const original = await popupPromise;
    await expect(original.locator("pre")).toHaveText(
      "Saved text: <script>not executable</script> 🧪\n",
    );
    await expect(original.locator("mark")).toHaveCount(0);
    await original.close();

    await page.getByRole("button", { name: "Refresh documents" }).click();
    await expect(
      page.getByRole("checkbox", {
        name: "Select document: Catalog source 1",
        exact: true,
      }),
    ).not.toBeChecked();
    await expect(
      page.getByRole("radio", { name: "Selected documents", exact: true }),
    ).toBeChecked();
    await expect(submit).toBeDisabled();
    await page
      .getByRole("radio", { name: "All saved documents", exact: true })
      .check();
    await submit.click();
    await expect(
      page.getByRole("heading", { name: "Insufficient evidence" }),
    ).toBeVisible();
    expect(submittedIds).toEqual([]);
  });
});
