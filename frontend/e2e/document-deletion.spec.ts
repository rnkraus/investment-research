import { expect, test, type BrowserContext } from "@playwright/test";

const FIRST = "10000000-0000-4000-8000-000000000001";
const SECOND = "10000000-0000-4000-8000-000000000002";
const VERSION = "10000000-0000-4000-8000-000000000099";

function document(
  id: string,
  title: string,
  state: "SEARCHABLE" | "FAILED" | "CAPTURED" | "LEXICAL_READY",
) {
  return {
    document_id: id,
    document_version_id: VERSION,
    title,
    language: "en",
    media_type: "text/plain",
    byte_size: 42,
    created_at: "2026-10-12T12:00:00Z",
    captured_at: "2026-10-12T12:00:00Z",
    published_at: null,
    available_at: null,
    processing: {
      processing_run_id: id,
      document_version_id: VERSION,
      state,
      error_summary: null,
    },
    original_url: `/api/v1/documents/${id}/versions/${VERSION}/original`,
  };
}

function receipt(id: string, complete: boolean) {
  return {
    document_id: id,
    state: complete ? "COMPLETED" : "PENDING",
    requested_at: "2026-10-12T12:00:00Z",
    completed_at: complete ? "2026-10-12T12:01:00Z" : null,
    status_url: `/api/v1/documents/${id}/deletion`,
  };
}

async function mockArchive(
  context: BrowserContext,
  excluded: () => boolean,
  documents = [
    document(FIRST, "Synthetic searchable source", "SEARCHABLE"),
    document(SECOND, "Synthetic failed source", "FAILED"),
  ],
) {
  await context.route("**/api/v1/research-requests/models", (route) =>
    route.fulfill({
      json: {
        provider_enabled: false,
        generation_models: [],
        support_models: [],
      },
    }),
  );
  await context.route("**/api/v1/documents?*", (route) =>
    route.fulfill({
      json: {
        items: excluded() ? [] : documents,
        next_cursor: null,
      },
    }),
  );
}

test("bulk deletion requires confirmation, clears stale answers, and waits for completed receipts", async ({
  page,
  context,
}) => {
  let admitted = false;
  let complete = false;
  let submissions = 0;
  await mockArchive(context, () => admitted);
  await context.route("**/api/v1/research-requests", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: `event: answer.insufficient\ndata: ${JSON.stringify({
        event: "answer.insufficient",
        schema_version: "1.0",
        research_request_id: VERSION,
        coverage: "INSUFFICIENT",
        message: "No answer in this synthetic test.",
      })}\n\n`,
    }),
  );
  await context.route("**/api/v1/documents/deletions", (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({
      document_ids: [FIRST, SECOND],
      confirm_permanent: true,
    });
    admitted = true;
    submissions += 1;
    return route.fulfill({
      status: 202,
      json: {
        items: [receipt(FIRST, false), receipt(SECOND, false)],
        dispatch_pending_document_ids: [],
      },
    });
  });
  await context.route("**/api/v1/documents/*/deletion", (route) => {
    const id = new URL(route.request().url()).pathname.split("/").at(-2)!;
    return route.fulfill({ json: receipt(id, complete) });
  });
  await page.goto("/");
  const ask = page.getByRole("button", {
    name: "Ask the archive",
    exact: true,
  });
  await expect(ask).toBeEnabled();
  await ask.click();
  const oldAnswer = page.getByRole("heading", {
    name: "Insufficient evidence",
  });
  await expect(oldAnswer).toBeVisible();
  const deleteSelected = page.getByRole("button", {
    name: "Delete selected",
    exact: true,
  });
  await expect(deleteSelected).toBeDisabled();
  await page
    .getByRole("checkbox", {
      name: "Select document: Synthetic searchable source",
      exact: true,
    })
    .check();
  await page
    .getByRole("checkbox", {
      name: "Select document: Synthetic failed source",
      exact: true,
    })
    .check();
  await expect(ask).toBeDisabled();
  await deleteSelected.click();
  await expect(
    page.getByRole("heading", {
      name: "Permanently delete selected documents?",
    }),
  ).toBeVisible();
  expect(submissions).toBe(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(submissions).toBe(0);
  await expect(oldAnswer).toBeVisible();
  await deleteSelected.click();
  await page
    .getByRole("button", { name: "Permanently delete", exact: true })
    .click();
  await expect(oldAnswer).toHaveCount(0);
  await expect(
    page.getByText("Confirmed cleanup: 0 / 2", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Close deletion status", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText(/Status updates automatically every five seconds/),
  ).toBeVisible();
  await expect(ask).toBeEnabled();
  complete = true;
  await expect(
    page.getByText("Confirmed cleanup: 2 / 2", { exact: true }),
  ).toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByRole("button", { name: "Close deletion status", exact: true }),
  ).toBeEnabled();
  expect(submissions).toBe(1);
  await expect(
    page.getByRole("checkbox", {
      name: "Select document: Synthetic searchable source",
      exact: true,
    }),
  ).toHaveCount(0);
});

for (const state of ["CAPTURED", "LEXICAL_READY"] as const) {
  test(`${state} can be selected for confirmed deletion but not research`, async ({
    page,
    context,
  }) => {
    let admitted = false;
    let submissions = 0;
    await mockArchive(context, () => admitted, [
      document(FIRST, "Synthetic waiting source", state),
    ]);
    await context.route("**/api/v1/documents/deletions", (route) => {
      expect(route.request().postDataJSON()).toEqual({
        document_ids: [FIRST],
        confirm_permanent: true,
      });
      admitted = true;
      submissions += 1;
      return route.fulfill({
        status: 202,
        json: {
          items: [receipt(FIRST, false)],
          dispatch_pending_document_ids: [],
        },
      });
    });
    await context.route(`**/api/v1/documents/${FIRST}/deletion`, (route) =>
      route.fulfill({ json: receipt(FIRST, true) }),
    );
    await page.goto("/");
    await page
      .getByRole("checkbox", {
        name: "Select document: Synthetic waiting source",
        exact: true,
      })
      .check();
    await expect(
      page.getByRole("button", { name: "Ask the archive", exact: true }),
    ).toBeDisabled();
    await page
      .getByRole("button", { name: "Delete selected", exact: true })
      .click();
    expect(submissions).toBe(0);
    await page
      .getByRole("button", { name: "Permanently delete", exact: true })
      .click();
    await expect(
      page.getByText("Confirmed cleanup: 1 / 1", { exact: true }),
    ).toBeVisible();
    expect(submissions).toBe(1);
  });
}

test("a Text extracted source that is still being indexed is rejected without claiming deletion", async ({
  page,
  context,
}) => {
  await mockArchive(context, () => false, [
    document(FIRST, "Synthetic leased source", "LEXICAL_READY"),
  ]);
  let submissions = 0;
  await context.route("**/api/v1/documents/deletions", (route) => {
    submissions += 1;
    return route.fulfill({ status: 409 });
  });
  await context.route("**/api/v1/documents/*/deletion", () => {
    throw new Error("A rejected batch must not start status tracking");
  });
  await page.goto("/");
  await page
    .getByRole("checkbox", {
      name: "Select document: Synthetic leased source",
      exact: true,
    })
    .check();
  await page
    .getByRole("button", { name: "Delete selected", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Permanently delete", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Processing may still be in progress",
  );
  await expect(
    page.getByRole("region", { name: "Permanent deletion", exact: true }),
  ).toHaveCount(0);
  expect(submissions).toBe(1);
});

test("an uncertain submission checks receipts instead of repeating the destructive request automatically", async ({
  page,
  context,
}) => {
  let admitted = false;
  let submissions = 0;
  await mockArchive(context, () => admitted);
  await context.route("**/api/v1/documents/deletions", (route) => {
    admitted = true;
    submissions += 1;
    return route.fulfill({
      status: 500,
      body: "Unstructured internal response must not be displayed",
    });
  });
  await context.route(`**/api/v1/documents/${FIRST}/deletion`, (route) =>
    route.fulfill({ json: receipt(FIRST, true) }),
  );
  await page.goto("/");
  await page
    .getByRole("checkbox", {
      name: "Select document: Synthetic searchable source",
      exact: true,
    })
    .check();
  await page
    .getByRole("button", { name: "Delete selected", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Permanently delete", exact: true })
    .click();
  await expect(
    page.getByText("Confirmed cleanup: 1 / 1", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Unstructured internal response must not be displayed"),
  ).toHaveCount(0);
  expect(submissions).toBe(1);
});

test("expired automatic tracking is explicit and a manual check refreshes the stale count", async ({
  page,
  context,
}) => {
  await page.clock.install();
  let admitted = false;
  let complete = false;
  let submissions = 0;
  await mockArchive(context, () => admitted);
  await context.route("**/api/v1/documents/deletions", (route) => {
    admitted = true;
    submissions += 1;
    return route.fulfill({
      status: 202,
      json: {
        items: [receipt(FIRST, false)],
        dispatch_pending_document_ids: [],
      },
    });
  });
  await context.route(`**/api/v1/documents/${FIRST}/deletion`, (route) =>
    route.fulfill({ json: receipt(FIRST, complete) }),
  );
  await page.goto("/");
  await page
    .getByRole("checkbox", {
      name: "Select document: Synthetic searchable source",
      exact: true,
    })
    .check();
  await page
    .getByRole("button", { name: "Delete selected", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Permanently delete", exact: true })
    .click();
  await expect(
    page.getByText(/Status updates automatically every five seconds/),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Ask the archive", exact: true }),
  ).toBeEnabled();
  await page.clock.fastForward(300_001);
  await expect(
    page.getByText(/Automatic status updates have stopped/),
  ).toBeVisible();
  await expect(
    page.getByText("Confirmed cleanup: 0 / 1", { exact: true }),
  ).toBeVisible();
  complete = true;
  await page
    .getByRole("button", { name: "Check deletion status", exact: true })
    .click();
  await expect(
    page.getByText("Confirmed cleanup: 1 / 1", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(/Automatic status updates have stopped/),
  ).toHaveCount(0);
  expect(submissions).toBe(1);
});
