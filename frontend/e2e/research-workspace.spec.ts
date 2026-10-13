import { expect, test } from "@playwright/test";

test.beforeEach(async ({ context }) => {
  await context.route("**/api/v1/documents?*", (route) =>
    route.fulfill({ json: { items: [], next_cursor: null } }),
  );
  await context.route("**/api/v1/research-requests/models", (route) =>
    route.fulfill({
      json: {
        provider_enabled: true,
        generation_models: ["gpt-5.6-terra", "gpt-5.6-luna"],
        support_models: ["gpt-5.6-terra", "gpt-5.6-luna"],
      },
    }),
  );
});

test("evidence rejection diagnostics are visible without expanding research details", async ({
  page,
  context,
}) => {
  const requestId = "10000000-0000-4000-8000-000000000001";
  const candidateId = "10000000-0000-4000-8000-000000000003";
  const events = [
    {
      event: "research.context",
      schema_version: "1.0",
      research_request_id: requestId,
      sources: [
        {
          retrieval_candidate_id: candidateId,
          chunk_id: "10000000-0000-7000-8000-000000000004",
          document_id: "10000000-0000-4000-8000-000000000005",
          document_version_id: "10000000-0000-4000-8000-000000000006",
          structure_block_id: "10000000-0000-4000-8000-000000000002",
          source_locator_kind: "TEXT_PASSAGE",
          page_number: null,
          text: "Revenue increased by 22.0%.",
        },
      ],
    },
    {
      event: "research.failed",
      schema_version: "1.0",
      research_request_id: requestId,
      error: {
        code: "EVIDENCE_VALIDATION_FAILED",
        message: "Answer failed evidence validation.",
        request_id: requestId,
      },
      retriable: false,
      diagnostic: {
        unvalidated: true,
        answer_model: "gpt-5.6-terra",
        support_model: "gpt-5.6-luna",
        statements: [
          {
            ordinal: 0,
            text: "An unvalidated claim.",
            evidence: [
              {
                retrieval_candidate_id: candidateId,
                start_character: 0,
                end_character: 25,
              },
            ],
          },
        ],
        findings: [
          {
            rule: "STATEMENT_SUPPORT",
            statement_ordinal: 0,
            reason:
              "Canonical evidence does not support the complete statement.",
          },
        ],
      },
    },
  ];
  await context.route("**/api/v1/research-requests", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: events
        .map(
          (event) =>
            `event: ${event.event}\ndata: ${JSON.stringify(event)}\n\n`,
        )
        .join(""),
    }),
  );
  await page.goto("/");
  await page.getByLabel("Research question").fill("What was the revenue?");
  const submit = page.getByRole("button", {
    name: "Ask the archive",
    exact: true,
  });
  await expect(submit).toBeEnabled();
  await submit.click();

  const failure = page.getByRole("article").filter({
    has: page.getByRole("heading", { name: "Research stopped safely" }),
  });
  await expect(
    failure.getByText("An unvalidated claim.", { exact: true }),
  ).toBeVisible();
  await expect(
    failure.getByText("Revenue increased by 22.0%.", { exact: true }),
  ).toBeVisible();
  await expect(
    failure.getByText(
      "Canonical evidence does not support the complete statement.",
      { exact: false },
    ),
  ).toBeVisible();
  const details = page.getByTestId("research-details");
  await expect(details).not.toHaveAttribute("open", "");
  await expect(
    details.getByText("An unvalidated claim.", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Validated answer", exact: true }),
  ).toHaveCount(0);
});

test("the workspace starts with research and preserves import drafts and keyboard focus", async ({
  page,
}) => {
  await page.goto("/");
  const addSource = page.getByRole("button", {
    name: "Add source",
    exact: true,
  });
  const question = page.getByLabel("Research question");
  await expect(question).toBeVisible();
  await expect(question).toHaveValue("");
  await expect(addSource).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByLabel("PDF document")).toBeHidden();
  await expect(page.getByText(/^Step [124]$/)).toHaveCount(0);
  await question.fill("What does this company do?");

  await addSource.click();
  await expect(addSource).toHaveAttribute("aria-expanded", "true");
  await expect(
    page.locator(`[id="${await addSource.getAttribute("aria-controls")}"]`),
  ).toBeFocused();
  await page.getByRole("radio", { name: "Paste text", exact: true }).check();
  await page.getByLabel("Research text").fill("Synthetic source text.");
  await page.getByLabel("Document title").fill("Synthetic draft");
  await page.getByLabel("Publication date").fill("2026-10-13");
  await page.getByRole("button", { name: "Close import", exact: true }).click();
  await expect(addSource).toBeFocused();
  await expect(page.getByLabel("Research text")).toBeHidden();
  await expect(question).toHaveValue("What does this company do?");

  await addSource.click();
  await expect(page.getByLabel("Research text")).toHaveValue(
    "Synthetic source text.",
  );
  await expect(page.getByLabel("Document title")).toHaveValue(
    "Synthetic draft",
  );
  await expect(page.getByLabel("Publication date")).toHaveValue("2026-10-13");
});

test("collapsed model settings preserve independent answer and evidence-check choices", async ({
  page,
}) => {
  await page.goto("/");
  const settings = page.getByTestId("model-settings");
  const summary = settings.locator(":scope > summary");
  await expect(settings).toBeVisible();
  await expect(settings).not.toHaveAttribute("open", "");
  await expect(page.getByLabel("Answer model", { exact: true })).toBeHidden();
  await summary.click();
  await page
    .getByLabel("Answer model", { exact: true })
    .selectOption("gpt-5.6-luna");
  await page
    .getByLabel("Evidence-check model", { exact: true })
    .selectOption("gpt-5.6-terra");
  await summary.click();
  await expect(summary).toContainText("GPT-5.6 Luna / GPT-5.6 Terra");
  await page
    .getByRole("button", { name: "Hide documents", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Documents", exact: true }),
  ).toBeHidden();
  await expect(
    page.getByRole("radio", { name: "All saved documents", exact: true }),
  ).toBeVisible();
  await summary.click();
  await expect(page.getByLabel("Answer model", { exact: true })).toHaveValue(
    "gpt-5.6-luna",
  );
  await expect(
    page.getByLabel("Evidence-check model", { exact: true }),
  ).toHaveValue("gpt-5.6-terra");
});

test("desktop columns become a collapsible single-column workspace on narrow screens", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/");
  const documents = page.getByRole("region", {
    name: "Documents",
    exact: true,
  });
  const research = page.getByRole("region", {
    name: "Ask the archive",
    exact: true,
  });
  await expect(documents).toBeVisible();
  await expect(research).toBeVisible();
  const sidebar = await documents.boundingBox();
  const main = await research.boundingBox();
  expect(sidebar).not.toBeNull();
  expect(main).not.toBeNull();
  expect(main!.x).toBeGreaterThanOrEqual(sidebar!.x + sidebar!.width);

  await page.setViewportSize({ width: 390, height: 844 });
  await page
    .getByRole("button", { name: "Hide documents", exact: true })
    .click();
  await expect(documents).toBeHidden();
  await expect(page.getByLabel("Research question")).toBeInViewport();
  const dimensions = await page.evaluate(() => ({
    content: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }));
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport);
  await page
    .getByRole("button", { name: "Show documents", exact: true })
    .click();
  await expect(documents).toBeVisible();
});
