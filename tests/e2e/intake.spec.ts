import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import type { Page } from "@playwright/test";

import { test, expect, signIn, ownNeed } from "./fixtures";

async function prepare(page: Page, file: string) {
  await page.getByLabel("Source file", { exact: true }).setInputFiles(file);
  const uploaded = page.waitForResponse((response) => response.url().endsWith("/sources") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Upload source", exact: true }).click();
  const source = (await (await uploaded).json()).source;
  const viewer = page.getByRole("region", { name: "Source viewer", exact: true });
  await viewer.getByRole("checkbox").check();
  await viewer.getByRole("button", { name: "Prepare reviewed source", exact: true }).click();
  return source;
}

const review = (page: Page) => page.getByRole("region", { name: "Record review", exact: true });

test("owner confirms a new mapping while staff cannot bypass the mapping gate", async ({ page, demo }) => {
  await signIn(page, demo, "owner-a", "inbox");
  const source = await prepare(page, "fixtures/sources/delivered-invoice.png");
  const card = review(page);
  await expect(card.getByText("Proposed · needs human confirmation", { exact: true })).toBeVisible();
  await card.getByText("Match a product specification", { exact: true }).click();
  const matching = card.getByRole("region", { name: "Match a product specification", exact: true });
  await matching.getByLabel("Product description from source", { exact: true }).fill("250ml paper cup with 80mm lid");
  await matching.getByLabel("Supplier item code", { exact: true }).fill("BROWSER-NEW-CUP");
  const requested = page.waitForResponse((response) => response.url().endsWith("/mappings") && response.request().method() === "POST");
  await matching.getByRole("button", { name: "Request matching", exact: true }).click();
  expect((await requested).status()).toBe(200);
  await expect(matching.getByRole("combobox", { name: "Product specification", exact: true })).toBeVisible();
  await matching.getByRole("combobox", { name: "Product specification", exact: true }).selectOption({ index: 1 });
  await matching.getByRole("combobox", { name: "Sale unit", exact: true }).selectOption("box");
  await matching.getByLabel("Pack factors (for example 20 × 50; a piece is 1)", { exact: true }).fill("100");
  await expect(matching.getByRole("button", { name: "Confirm product mapping", exact: true })).toBeDisabled();
  await matching.getByRole("checkbox").check();
  await matching.getByRole("button", { name: "Confirm product mapping", exact: true }).click();
  await expect(matching.getByText("Owner-confirmed. Edit the record and select this mapping before confirming the record.", { exact: true })).toBeVisible();
  const { business } = await ownNeed(page);
  const root = `/api/v1/businesses/${business}`;
  const rows = await (await page.request.get(`${root}/mappings`)).json();
  const mapping = rows.items.find((item: { supplier_sku: string }) => item.supplier_sku === "BROWSER-NEW-CUP");
  expect(mapping.confirmed).toBe(true);
  expect(mapping.source_id).toBe(source.id);
  expect(mapping.pack_factors).toEqual(["100"]);
  const options = await (await page.request.get(`${root}/record-options`)).json();
  expect(options.mappings.some((item: { id: string }) => item.id === mapping.id)).toBe(true);
  await signIn(page, demo, "staff", "inbox");
  const denied = await page.request.post(`${root}/mappings/${mapping.id}/confirm`, {
    headers: { Origin: new URL(page.url()).origin, "Idempotency-Key": crypto.randomUUID() },
    data: { expected_revision: mapping.revision, spec_id: mapping.spec_id, spec_revision: mapping.spec_revision,
      source_unit: "box", pack_factors: ["100"], acknowledge_flags: true },
  });
  expect(denied.status()).toBe(403);
});

for (const purpose of ["Historical purchase", "Delivered invoice"]) {
  test(`${purpose} source becomes one accepted record only after human review`, async ({ page, demo }, testInfo) => {
    await signIn(page, demo, "owner-a", "inbox");
    const source = await prepare(page, purpose === "Historical purchase" ? "fixtures/sources/delivered-invoice.png" : "fixtures/sources/delivered-invoice.pdf");
    const card = review(page);
    await expect(card.getByText("Proposed · needs human confirmation", { exact: true })).toBeVisible();
    await expect(card.getByRole("button", { name: "Confirm record", exact: true })).toBeDisabled();
    const { business, need } = await ownNeed(page);
    const root = `/api/v1/businesses/${business}`;
    const before = await (await page.request.get(`${root}/proposals`)).json();
    const original = before.items.find((item: { source_id: string }) => item.source_id === source.id);
    expect(original.record_id).toBeNull();
    expect(original.reading.lines[0].unit_price).toBe("0.275");
    const priorLedger = await (await page.request.get(`${root}/ledger?kind=purchase`)).json();
    expect(priorLedger.items.some((item: { source_id: string }) => item.source_id === source.id)).toBe(false);

    await card.getByRole("button", { name: "Item 1 · Printed unit price · 0.275", exact: true }).click();
    const crop = card.getByRole("img", { name: "Source crop", exact: true });
    await expect(crop).toBeVisible();
    await crop.evaluate((element) => (element as HTMLImageElement).decode());
    await crop.screenshot({ path: testInfo.outputPath("original-price-evidence.png") });
    await card.getByRole("button", { name: "Item 1 · Printed unit price · 0.275", exact: true }).click();

    await card.getByRole("button", { name: "Edit record", exact: true }).click();
    const form = card.getByRole("form", { name: "Edit record", exact: true });
    await form.getByRole("combobox", { name: "Purchase purpose", exact: true }).selectOption({ label: purpose });
    if (purpose === "Historical purchase") await form.getByRole("combobox", { name: "Confirmed item mapping", exact: true }).selectOption({ index: 1 });
    await expect(form.getByLabel("Printed discount", { exact: true })).toHaveValue("");
    await form.getByRole("button", { name: "Save corrections", exact: true }).click();
    await expect(card.getByText("Revision 2", { exact: true })).toBeVisible();
    await expect(card.getByText("Printed discount · Unknown", { exact: true })).toBeVisible();
    if (purpose === "Delivered invoice") await expect(card.getByText("Unmapped · excluded from buying comparisons", { exact: true })).toBeVisible();
    await card.getByRole("checkbox").check();
    await card.getByRole("button", { name: "Confirm record", exact: true }).click();
    await expect(card.getByRole("status").filter({ hasText: /^Record confirmed$/ })).toBeVisible();
    const current = await (await page.request.get(`${root}/proposals/${original.id}`)).json();
    expect(current.status).toBe("confirmed");
    expect(current.record_id).toBeTruthy();
    expect(current.reading.discount).toBeNull();
    const ledger = await (await page.request.get(`${root}/ledger?kind=purchase`)).json();
    expect(ledger.items.filter((item: { id: string }) => item.id === current.record_id)).toHaveLength(1);
    expect((await page.request.get(`${root}/needs/${need}/order`)).status()).toBe(404);
    const duplicate = await page.request.post(`${root}/proposals/${original.id}/confirm`, {
      headers: { Origin: new URL(page.url()).origin, "Idempotency-Key": crypto.randomUUID() },
      data: { expected_revision: 2, acknowledged_flags: current.flags },
    });
    expect(duplicate.status()).toBe(200);
    expect((await duplicate.json()).record_id).toBe(current.record_id);
    await page.reload();
    await page.getByRole("button", { name: "View source", exact: true }).first().click();
    await expect(review(page).getByRole("status").filter({ hasText: /^Record confirmed$/ })).toBeVisible();
    await review(page).screenshot({ path: testInfo.outputPath("confirmed-record.png") });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    if (purpose === "Historical purchase") for (const locale of ["ms", "zh"]) {
      const translated = JSON.parse(readFileSync(`apps/web/src/messages/${locale}.json`, "utf8"));
      await page.goto(`/${locale}/inbox`);
      await page.getByRole("button", { name: translated.viewSource, exact: true }).first().click();
      const localizedCard = page.getByRole("region", { name: translated.records.review, exact: true });
      await expect(localizedCard.getByRole("status").filter({ hasText: translated.records.status.confirmed })).toBeVisible();
      await expect(localizedCard.getByRole("button", { name: translated.records.editRecord, exact: true })).toHaveCount(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }
  });
}

test("stock time and mapping need human input and restricted staff see no record values", async ({ page, demo }, testInfo) => {
  await signIn(page, demo, "owner-a", "inbox");
  await prepare(page, "fixtures/sources/stock-ms.txt");
  const card = review(page);
  await expect(card.getByRole("heading", { name: "Stock record", exact: true })).toBeVisible();
  await card.getByRole("button", { name: "Edit record", exact: true }).click();
  const form = card.getByRole("form", { name: "Edit record", exact: true });
  const time = form.getByLabel("Observation time (Malaysia, UTC+8)", { exact: true });
  await expect(time).toHaveValue("");
  await form.getByRole("combobox", { name: "Confirmed item mapping", exact: true }).selectOption({ index: 1 });
  await form.getByLabel("Observed quantity", { exact: true }).fill("0");
  await time.fill(new Date(Date.now() + 8 * 3600000 - 60000).toISOString().slice(0, 16));
  await form.getByRole("button", { name: "Save corrections", exact: true }).click();
  await expect(card.getByText("Revision 2", { exact: true })).toBeVisible();
  await card.getByRole("checkbox").check();
  await card.getByRole("button", { name: "Confirm record", exact: true }).click();
  await expect(card.getByRole("status").filter({ hasText: /^Record confirmed$/ })).toBeVisible();
  await card.screenshot({ path: testInfo.outputPath("stock-confirmed.png") });
  await signIn(page, demo, "staff", "inbox");
  await page.getByRole("button", { name: "View source", exact: true }).first().click();
  await expect(page.getByText("This account cannot view price-bearing sources.", { exact: true })).toBeVisible();
  await expect(review(page)).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText(/RM\s*[0-9]/);
});

test("an edited mismatch keeps original evidence and rejection survives reload", async ({ page, demo }) => {
  await signIn(page, demo, "owner-a", "inbox");
  await prepare(page, "fixtures/sources/delivered-invoice.png");
  const card = review(page);
  await card.getByRole("button", { name: "Edit record", exact: true }).click();
  const form = card.getByRole("form", { name: "Edit record", exact: true });
  await form.getByRole("combobox", { name: "Purchase purpose", exact: true }).selectOption("historical");
  await form.getByLabel("Printed unit price", { exact: true }).fill("0.270");
  await form.getByRole("button", { name: "Save corrections", exact: true }).click();
  await expect(card.getByText("Item 1 · Printed item total · Printed and calculated values differ", { exact: true })).toBeVisible();
  await expect(card.getByRole("button", { name: "Item 1 · Printed unit price · 0.275", exact: true })).toBeVisible();
  await card.getByRole("button", { name: "Reject proposal", exact: true }).click();
  await expect(card.getByRole("status").filter({ hasText: /^Proposal rejected$/ })).toBeVisible();
  await expect(card.getByRole("button", { name: "Confirm record", exact: true })).toHaveCount(0);
  await page.reload();
  await page.getByRole("button", { name: "View source", exact: true }).first().click();
  await expect(review(page).getByRole("status").filter({ hasText: /^Proposal rejected$/ })).toBeVisible();
});

test("staff over the configured limit must leave the saved proposal for its owner", async ({ page, demo }) => {
  await signIn(page, demo, "owner-a", "inbox");
  await prepare(page, "fixtures/sources/delivered-invoice.pdf");
  await expect(review(page)).toBeVisible();
  const { business } = await ownNeed(page);
  execFileSync("uv", ["run", "--project", "services/api", "python", "-c", `
import asyncio,sys
from uuid import UUID
from dotenv import dotenv_values
from psycopg import AsyncConnection
from collective.repositories.db import require_local_database
async def configure():
    async with await AsyncConnection.connect(require_local_database(dotenv_values('.env')['MIGRATION_DATABASE_URL'])) as conn:
        await conn.execute('update collective.businesses set staff_can_view_prices=true,staff_limit=289.99 where id=%s',(UUID(sys.argv[1]),))
asyncio.run(configure())
`, business], { stdio: "pipe" });
  await signIn(page, demo, "staff", "inbox");
  await page.getByRole("button", { name: "View source", exact: true }).first().click();
  const card = review(page);
  await card.getByRole("button", { name: "Edit record", exact: true }).click();
  const form = card.getByRole("form", { name: "Edit record", exact: true });
  await form.getByRole("combobox", { name: "Purchase purpose", exact: true }).selectOption("historical");
  await form.getByRole("combobox", { name: "Confirmed item mapping", exact: true }).selectOption({ index: 1 });
  await form.getByLabel("Printed discount", { exact: true }).fill("0.00");
  await form.getByLabel("Printed surcharge", { exact: true }).fill("0.00");
  await form.getByLabel("Tax rate (0–1)", { exact: true }).fill("0");
  await form.getByRole("combobox", { name: "Delivery arrangement", exact: true }).selectOption("per_business");
  await form.getByRole("button", { name: "Save corrections", exact: true }).click();
  await expect(card.getByText("RM 290.00", { exact: true })).toBeVisible();
  await card.getByRole("checkbox").check();
  await card.getByRole("button", { name: "Confirm record", exact: true }).click();
  await expect(card.getByRole("alert")).toHaveText("Owner review is required: the value is unknown, inconsistent or above the staff limit.");
  await signIn(page, demo, "owner-a", "inbox");
  await page.getByRole("button", { name: "View source", exact: true }).first().click();
  await expect(review(page).getByText("Revision 2", { exact: true })).toBeVisible();
  await review(page).getByRole("checkbox").check();
  await review(page).getByRole("button", { name: "Confirm record", exact: true }).click();
  await expect(review(page).getByRole("status").filter({ hasText: /^Record confirmed$/ })).toBeVisible();
});

test("a replay miss stops visibly and retry keeps the original failure", async ({ page, demo }) => {
  await signIn(page, demo, "owner-a", "inbox");
  await page.getByLabel("Or paste invoice, stock or supplier text", { exact: true }).fill("SYNTHETIC unlisted text for the intake recovery check.");
  const uploaded = page.waitForResponse((response) => response.url().endsWith("/sources") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Upload source", exact: true }).click();
  const source = (await (await uploaded).json()).source;
  const viewer = page.getByRole("region", { name: "Source viewer", exact: true });
  await viewer.getByRole("checkbox").check();
  await viewer.getByRole("button", { name: "Prepare reviewed source", exact: true }).click();
  const reading = page.getByRole("region", { name: "Source reading", exact: true });
  await expect(reading.getByRole("status")).toContainText("No exact replay is available");
  const { business } = await ownNeed(page);
  const root = `/api/v1/businesses/${business}`;
  const before = await (await page.request.get(`${root}/sources/${source.id}`)).json();
  const requested = page.waitForResponse((response) => response.url().endsWith("/intake") && response.request().method() === "POST");
  await reading.getByRole("button", { name: "Retry source reading", exact: true }).click();
  const next = await (await requested).json();
  expect(next.id).not.toBe(before.job.id);
  await expect(reading.getByRole("status")).toContainText("No exact replay is available");
  const original = await (await page.request.get(`${root}/jobs/${before.job.id}`)).json();
  expect(original.status).toBe("failed");
  expect(original.error_code).toBe("REPLAY_MISS");
  await expect(review(page)).toHaveCount(0);
});
