import { expect, test } from "@playwright/test";

const DOCUMENT = "10000000-0000-4000-8000-000000000001";
const VERSION = "10000000-0000-4000-8000-000000000002";
const FIRST = "10000000-0000-4000-8000-000000000003";
const SECOND = "10000000-0000-4000-8000-000000000004";
const CREATED_AT = "2026-11-05T12:00:00Z";
const initialCompany = {
  company_id: FIRST,
  name: "Northstar Grid",
  ticker: null as string | null,
  created_at: CREATED_AT,
};
const created = {
  company_id: SECOND,
  name: "Blue Ridge",
  ticker: "BLRG",
  created_at: CREATED_AT,
};

test("a document can save company selections and recover from a conflicting edit", async ({
  page,
  context,
}) => {
  let revision = 0;
  let known = initialCompany;
  let companies: (typeof initialCompany)[] = [];
  const writes: unknown[] = [];
  const path = `/api/v1/documents/${DOCUMENT}/companies`;
  const snapshot = () => ({
    document_id: DOCUMENT,
    document_version_id: VERSION,
    revision,
    companies,
  });

  await context.route("**/api/v1/documents?*", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            document_id: DOCUMENT,
            document_version_id: VERSION,
            title: "Synthetic source",
            language: "en",
            media_type: "text/plain",
            byte_size: 42,
            created_at: CREATED_AT,
            captured_at: CREATED_AT,
            processing: null,
            original_url: `/api/v1/documents/${DOCUMENT}/versions/${VERSION}/original`,
          },
        ],
        next_cursor: null,
      },
    }),
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
  await context.route("**/api/v1/companies?*", (route) => {
    expect(new URL(route.request().url()).searchParams.get("q")).toBe(
      "Northstar",
    );
    return route.fulfill({ json: { items: [known], next_cursor: null } });
  });
  await context.route("**/api/v1/companies", (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({
      name: "Blue Ridge",
      ticker: "BLRG",
    });
    return route.fulfill({ status: 201, json: created });
  });
  await context.route(`**/api/v1/companies/${FIRST}/ticker`, (route) => {
    expect(route.request().method()).toBe("PATCH");
    expect(route.request().postDataJSON()).toEqual({ ticker: "NGRID" });
    known = { ...known, ticker: "NGRID" };
    return route.fulfill({ json: known });
  });
  await context.route(`**${path}`, (route) => {
    if (route.request().method() === "GET")
      return route.fulfill({ json: snapshot() });
    expect(route.request().method()).toBe("PUT");
    const command = route.request().postDataJSON();
    writes.push(command);
    if (command.expected_revision !== revision) {
      return route.fulfill({
        status: 409,
        json: { error: { code: "CONFLICT", message: "Changed" } },
      });
    }
    companies = [known, created].filter((company) =>
      command.company_ids.includes(company.company_id),
    );
    revision += 1;
    return route.fulfill({ json: snapshot() });
  });

  await page.goto("/");
  await page
    .getByRole("button", { name: "Edit companies for Synthetic source" })
    .click();
  const editor = page.getByRole("region", {
    name: "Companies for Synthetic source",
  });
  await editor.getByLabel("Search companies").fill("Northstar");
  await editor.getByRole("button", { name: "Search", exact: true }).click();
  await editor
    .getByRole("button", { name: "Edit ticker for Northstar Grid" })
    .click();
  await editor.getByLabel("Ticker for Northstar Grid").fill("NGRID");
  await editor.getByRole("button", { name: "Save ticker" }).click();
  await expect(editor.getByText("Northstar Grid (NGRID)")).toBeVisible();
  await editor.getByRole("button", { name: "Select Northstar Grid" }).click();
  await editor.getByLabel("New company name").fill("Blue Ridge");
  await editor.getByLabel("Ticker (optional)").fill("BLRG");
  await editor.getByRole("button", { name: "Create and select" }).click();
  await expect(
    editor.getByRole("button", { name: "Remove Blue Ridge" }),
  ).toBeVisible();
  await expect(editor.getByText("Blue Ridge (BLRG)")).toBeVisible();
  expect(writes).toHaveLength(0);
  const save = editor.getByRole("button", { name: "Save companies" });
  await save.click();
  await expect(editor.getByText("Company assignments saved.")).toBeVisible();
  expect(writes[0]).toEqual({
    expected_document_version_id: VERSION,
    expected_revision: 0,
    company_ids: [FIRST, SECOND],
  });

  revision = 2;
  await editor.getByRole("button", { name: "Remove Blue Ridge" }).click();
  await save.click();
  await expect(editor.getByRole("alert")).toContainText(
    "Reload before saving again",
  );
  await expect(save).toBeDisabled();
  await editor.getByRole("button", { name: "Reload assignments" }).click();
  await expect(
    editor.getByRole("button", { name: "Remove Blue Ridge" }),
  ).toBeVisible();
  await expect(editor.getByRole("alert")).toHaveCount(0);
});
