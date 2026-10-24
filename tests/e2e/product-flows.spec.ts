import { test, expect, signIn, openScreen } from "./fixtures";

test("five mobile screens use persisted records and preserve business context", async ({ page, demo }, testInfo) => {
  await signIn(page, demo, "owner-a");
  await expect(page.getByRole("region", { name: "Sources", exact: true })).toHaveCount(0);
  await openScreen(page, "inbox");
  await expect(page.getByRole("region", { name: "Sources", exact: true })).toBeVisible();
  await openScreen(page, "ledger");
  const audit = page.getByRole("region", { name: "Self-audit", exact: true });
  await expect(audit.getByText("Recorded history only. A discrepancy is not recovered money. Latest confirmed record revisions are counted once.", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Recorded ledger", exact: true })).toContainText("RM 315.00");
  await page.getByRole("combobox", { name: "Record type", exact: true }).selectOption("po");
  await expect(page.getByText("No records of this type yet.", { exact: false })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("mobile-ledger-audit.png"), fullPage: true });
  await openScreen(page, "rules");
  await expect(page.getByRole("region", { name: "Learned rules", exact: true })).toBeVisible();
  await openScreen(page, "activity");
  await expect(page.getByRole("region", { name: "Background tasks", exact: true })).toBeVisible();
  await openScreen(page, "approvals");
  await expect(page.getByRole("heading", { name: "250ml paper cup", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
