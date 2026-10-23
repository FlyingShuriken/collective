import { test, expect, approve, ownOrder, readyPool, signIn } from "./fixtures";

test("three approvals, withdrawal and fresh consent issue two final RM285 POs", async ({ page, demo }, testInfo) => {
  await readyPool(page, demo);
  await page.getByRole("button", { name: "Decline / withdraw from pool", exact: true }).click();
  await expect(page.getByText("Selected · quantity fixed", { exact: true })).toBeVisible();
  await signIn(page, demo, "owner-a");
  await expect(page.getByText("Your previous approval is void. Fresh consent is required.", { exact: true })).toBeVisible();
  await expect(page.getByText("Saved maximum cap: RM 276.00", { exact: true })).toBeVisible();
  expect((await ownOrder(page)).status()).toBe(404);
  await approve(page, "285.00");
  await page.getByRole("button", { name: "Create simulated POs", exact: true }).click();
  const po = page.getByRole("region", { name: "Simulated purchase order", exact: true });
  await expect(po.getByText("Order issued", { exact: true })).toBeVisible();
  const a = await (await ownOrder(page)).json();
  expect(a.amount).toBe("285.00");
  expect(a.cost.unit_price).toBe("0.270000");
  await expect(page.getByRole("region", { name: "Activity", exact: true }).getByText(
    "A simulated purchase order was created from your current bound approval.", { exact: true },
  )).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await po.screenshot({ path: testInfo.outputPath("issued-pool-po.png") });
  await page.reload();
  await expect(po.getByText(`Order reference: ${a.id}`, { exact: true })).toBeVisible();
  expect(await (await ownOrder(page)).json()).toEqual(a);
  await signIn(page, demo, "owner-b");
  await expect(po.getByText("Order issued", { exact: true })).toBeVisible();
  const b = await (await ownOrder(page)).json();
  expect(b.amount).toBe("285.00");
  expect(b.id).not.toBe(a.id);
  await signIn(page, demo, "owner-c");
  expect((await ownOrder(page)).status()).toBe(404);
  await expect(page.getByRole("button", { name: "Create simulated POs", exact: true })).toHaveCount(0);
});

test("alternative decline leaves B void at RM315 and no PO", async ({ page, demo }) => {
  await readyPool(page, demo);
  await page.getByRole("button", { name: "Decline / withdraw from pool", exact: true }).click();
  await expect(page.getByText("Selected · quantity fixed", { exact: true })).toBeVisible();
  await signIn(page, demo, "owner-a");
  await expect(page.getByText("Your previous approval is void. Fresh consent is required.", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Decline / withdraw from pool", exact: true }).click();
  await expect(page.getByText("Selected · quantity fixed", { exact: true })).toBeVisible();
  await signIn(page, demo, "owner-b");
  const pool = page.getByRole("region", { name: "Shared buying option", exact: true });
  await expect(pool.getByText("Your previous approval is void. Fresh consent is required.", { exact: true })).toBeVisible();
  await expect(pool.getByText("RM 315.00", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Create simulated POs", exact: true })).toHaveCount(0);
  expect((await ownOrder(page)).status()).toBe(404);
});

test("solo approval issues one simulated PO and staff cannot see its prices", async ({ page, demo }, testInfo) => {
  await signIn(page, demo, "owner-a");
  await page.getByRole("button", { name: "Review solo order", exact: true }).first().click();
  await approve(page, "315.00");
  expect((await ownOrder(page)).status()).toBe(404);
  await page.getByRole("button", { name: "Create simulated PO", exact: true }).click();
  const po = page.getByRole("region", { name: "Simulated purchase order", exact: true });
  await expect(po.getByText("Order issued", { exact: true })).toBeVisible();
  const order = await (await ownOrder(page)).json();
  expect(order.amount).toBe("315.00");
  expect(order.implementation).toBe("simulated");
  await po.screenshot({ path: testInfo.outputPath("issued-solo-po.png") });
  await signIn(page, demo, "staff");
  await expect(po.getByText("Prices are hidden for this staff account.", { exact: true })).toBeVisible();
  const hidden = await (await ownOrder(page)).json();
  expect(hidden.price_visibility).toBe("hidden");
  for (const field of ["amount", "decision_amount", "terms", "cost", "supplier_id"]) expect(hidden).not.toHaveProperty(field);
  await expect(page.locator("body")).not.toContainText(/RM\s*[0-9]/);
  await expect(page.getByRole("button", { name: /^(Approve these terms|Create simulated PO)$/ })).toHaveCount(0);
});
