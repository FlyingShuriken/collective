import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { test, expect, signIn, ownNeed, ownOrder, approve } from "./fixtures";
import type { Page } from "@playwright/test";

async function upload(page: Page, file?: string, content?: string) {
  if (file) await page.getByLabel("Source file", { exact: true }).setInputFiles(file);
  else await page.getByLabel("Or paste invoice, stock or supplier text", { exact: true }).fill(content!);
  const response = page.waitForResponse((value) => value.url().endsWith("/sources") && value.request().method() === "POST");
  await page.getByRole("button", { name: "Upload source", exact: true }).click();
  const source = (await (await response).json()).source;
  const viewer = page.getByRole("region", { name: "Source viewer", exact: true });
  await viewer.getByRole("checkbox").check();
  await viewer.getByRole("button", { name: "Prepare reviewed source", exact: true }).click();
  return source;
}

async function confirm(page: Page, purpose: "historical" | "delivered") {
  const card = page.getByRole("region", { name: "Record review", exact: true });
  await card.getByRole("button", { name: "Edit record", exact: true }).click();
  const editor = card.getByRole("form", { name: "Edit record", exact: true });
  await editor.getByRole("combobox", { name: "Purchase purpose", exact: true }).selectOption(purpose);
  await editor.getByRole("combobox", { name: "Confirmed item mapping", exact: true }).selectOption({ index: 1 });
  await editor.getByRole("button", { name: "Save corrections", exact: true }).click();
  await expect(card.getByText("Revision 2", { exact: true })).toBeVisible();
  await card.getByRole("checkbox").check();
  await card.getByRole("button", { name: "Confirm record", exact: true }).click();
  await expect(card.getByRole("status").filter({ hasText: /^Record confirmed$/ })).toBeVisible();
  return card;
}

test("one business confirms sources, orders, checks the later invoice and reviews a supplier query", async ({ page, demo }, testInfo) => {
  await signIn(page, demo, "owner-a");
  await upload(page, "fixtures/sources/delivered-invoice.png");
  await confirm(page, "historical");
  expect((await ownOrder(page)).status()).toBe(404);
  const supplier = page.getByRole("heading", { name: "Supplier B", exact: true }).locator("..");
  await supplier.getByRole("button", { name: "Review solo order", exact: true }).click();
  await approve(page, "315.00");
  expect((await ownOrder(page)).status()).toBe(404);
  await page.getByRole("button", { name: "Create simulated PO", exact: true }).click();
  await expect(page.getByRole("region", { name: "Simulated purchase order", exact: true }).getByRole("status").filter({ hasText: /^Order issued$/ })).toBeVisible();
  const order = await (await ownOrder(page)).json();
  expect(order.amount).toBe("315.00");
  const invoiceSource = await upload(page, "fixtures/sources/single-business-invoice.txt");
  const card = await confirm(page, "delivered");
  const invoice = card.getByRole("region", { name: "Invoice check", exact: true });
  await expect(invoice.getByRole("status").filter({ hasText: /^Unmatched · not passed$/ })).toBeVisible();
  await invoice.getByRole("combobox", { name: "Issued PO for this invoice", exact: true }).selectOption(order.id);
  await invoice.getByRole("button", { name: "Check invoice against selected PO", exact: true }).click();
  await expect(invoice.getByRole("status").filter({ hasText: /^Checked · differences need review$/ })).toBeVisible();
  await expect(invoice.getByText("RM 5.00", { exact: true }).first()).toBeVisible();
  await expect(invoice.getByText("No documented recovery", { exact: true })).toBeVisible();
  const ownerReview = invoice.getByRole("form", { name: "Owner discrepancy review", exact: true });
  await ownerReview.getByLabel("Owner-confirmed discrepancy amount (RM)", { exact: true }).fill("5.00");
  await ownerReview.getByRole("checkbox").check();
  await ownerReview.getByRole("button", { name: "Confirm discrepancy", exact: true }).click();
  await expect(invoice.getByRole("status").filter({ hasText: /^Discrepancy confirmed · no recovery yet$/ })).toBeVisible();
  const { business } = await ownNeed(page);
  const root = `/api/v1/businesses/${business}`;
  const proposals = await (await page.request.get(`${root}/proposals`)).json();
  const record = proposals.items.find((value: { source_id: string }) => value.source_id === invoiceSource.id);
  const checks = await (await page.request.get(`${root}/invoice-checks?purchase_id=${record.record_id}`)).json();
  const check = checks.items.find((value: { status: string; po_id: string }) => value.status === "flagged" && value.po_id === order.id);
  const preload = ["run", "--project", "services/api", "python", "scripts/preload_query_replay.py", "--run", demo.run, "--check-id", check.id];
  expect(() => execFileSync("uv", [...preload.slice(0, 5), "--run", "wrong-synthetic-run", "--check-id", check.id], { stdio: "pipe", env: { ...process.env, DEMO_MODE: "true", MODEL_MODE: "replay" } })).toThrow();
  const manifestBefore = readFileSync("fixtures/replay/manifest.json", "utf8");
  const authored = JSON.parse(execFileSync("uv", preload, { stdio: "pipe", env: { ...process.env, DEMO_MODE: "true", MODEL_MODE: "replay" } }).toString());
  try {
    await invoice.getByRole("button", { name: "Prepare supplier query", exact: true }).click();
    const query = invoice.getByRole("region", { name: "Supplier query", exact: true });
    await expect(query.getByRole("status").filter({ hasText: /^Draft · not sent$/ })).toBeVisible();
    await expect(query.getByRole("button", { name: "Send simulated query", exact: true })).toBeDisabled();
    const editor = query.getByRole("form", { name: "Edit supplier query", exact: true });
    const message = editor.getByRole("textbox", { name: "Query text", exact: true });
    expect(await message.inputValue()).toContain("RM 5.00");
    await message.fill((await message.inputValue()) + " Please reply with the credit reference.");
    await expect(query.getByRole("checkbox")).toBeDisabled();
    await editor.getByRole("button", { name: "Save query changes", exact: true }).click();
    await query.getByRole("checkbox").check();
    const sent = page.waitForResponse((value) => value.url().endsWith("/send") && value.request().method() === "POST");
    await query.getByRole("button", { name: "Send simulated query", exact: true }).click();
    const receipt = await (await sent).json();
    expect(receipt.simulated).toBe(true);
    await expect(query.getByRole("status").filter({ hasText: /^Query sent through simulated channel$/ })).toBeVisible();
    await expect(invoice.getByText("No documented recovery", { exact: true })).toBeVisible();
    await invoice.screenshot({ path: testInfo.outputPath("checked-invoice-and-sent-query.png") });
    await page.reload();
    await page.getByRole("listitem").filter({ hasText: invoiceSource.id }).getByRole("button", { name: "View source", exact: true }).click();
    await expect(page.getByRole("region", { name: "Supplier query", exact: true }).getByRole("status").filter({ hasText: /^Query sent through simulated channel$/ })).toBeVisible();
    const credit = await upload(page, undefined, "SYNTHETIC Supplier B credit note CN-SINGLE-002. Credit MYR5.00 for invoice SINGLE-B-002 following agreed correction. No customer or bank details.");
    await page.getByRole("listitem").filter({ hasText: invoiceSource.id }).getByRole("button", { name: "View source", exact: true }).click();
    const restored = page.getByRole("region", { name: "Invoice check", exact: true });
    await restored.getByRole("combobox", { name: "Reviewed correction / credit source", exact: true }).selectOption(credit.id);
    const recovery = restored.getByRole("form", { name: "Record documented recovery", exact: true });
    await recovery.getByLabel("Correction / credit reference", { exact: true }).fill("CN-SINGLE-002");
    await recovery.getByLabel("Documented recovered amount (RM)", { exact: true }).fill("5.00");
    await recovery.getByRole("checkbox").check();
    await recovery.getByRole("button", { name: "Record documented recovery", exact: true }).click();
    await expect(restored.getByRole("status").filter({ hasText: /^Documented correction or credit recorded$/ })).toBeVisible();
    const saved = await (await page.request.get(`${root}/invoice-checks/${check.id}`)).json();
    expect(saved.confirmed_amount).toBe("5.00");
    expect(saved.recovery_amount).toBe("5.00");
    expect(saved.resolution.source_id).toBe(credit.id);
    expect((await (await ownOrder(page)).json()).amount).toBe("315.00");
    const learning = page.getByRole("region", { name: "Learned rules", exact: true });
    await learning.getByRole("button", { name: "Generate rule proposals", exact: true }).click();
    const accuracy = learning.getByRole("article", { name: "Quote accuracy", exact: true }).filter({ has: page.getByRole("status").filter({ hasText: /^Proposed · not active$/ }) });
    await expect(accuracy.getByText("Material discrepancies: 1 / 1", { exact: true })).toBeVisible();
    await accuracy.getByRole("checkbox").check();
    await accuracy.getByRole("button", { name: "Accept rule", exact: true }).click();
    const warning = page.getByRole("heading", { name: "Supplier B", exact: true }).locator("..").getByRole("complementary", { name: "Accepted invoice-check history", exact: true });
    await expect(warning.getByText(/Material discrepancies: 1 \/ 1/)).toBeVisible();
    const afterRule = await (await page.request.get(`${root}/invoice-checks/${check.id}`)).json();
    expect(afterRule.result).toEqual(saved.result);
    expect(afterRule.recovery_amount).toBe("5.00");
    expect((await (await ownOrder(page)).json()).amount).toBe("315.00");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    for (const locale of ["ms", "zh"]) {
      const copy = JSON.parse(readFileSync(`apps/web/src/messages/${locale}.json`, "utf8"));
      await page.goto(`/${locale}/approvals`);
      await page.getByRole("listitem").filter({ hasText: invoiceSource.id }).getByRole("button", { name: copy.viewSource, exact: true }).click();
      const localized = page.getByRole("region", { name: copy.invoices.heading, exact: true });
      await expect(localized.getByRole("status").filter({ hasText: copy.invoices.recovered })).toBeVisible();
      await expect(localized.getByRole("region", { name: copy.invoices.query, exact: true }).getByRole("status").filter({ hasText: copy.invoices.sent })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }
    await signIn(page, demo, "staff");
    await page.getByRole("listitem").filter({ hasText: invoiceSource.id }).getByRole("button", { name: "View source", exact: true }).click();
    await expect(page.getByRole("region", { name: "Invoice check", exact: true })).toHaveCount(0);
    await expect(page.locator("body")).not.toContainText(/RM\s*[0-9]/);
  } finally {
    execFileSync("uv", [...preload, "--clean", "--source-hash", authored.source_hash], { stdio: "pipe", env: { ...process.env, DEMO_MODE: "true", MODEL_MODE: "replay" } });
    expect(readFileSync("fixtures/replay/manifest.json", "utf8")).toBe(manifestBefore);
  }
});
