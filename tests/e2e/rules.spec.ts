import { execFileSync } from "node:child_process";
import type { Page } from "@playwright/test";
import { test, expect, signIn, ownNeed, ownOrder, approve, openScreen } from "./fixtures";

function prepare(run: string, mode: "private" | "private-more" | "network") {
  return JSON.parse(execFileSync("uv", ["run", "--project", "services/api", "python", "tests/e2e/prepare_learning.py", "--run", run, "--mode", mode], { stdio: "pipe" }).toString()) as { business_id: string; private_ids: string[] };
}

function rule(page: Page, kind: string, state: string) {
  return page.getByRole("article", { name: kind, exact: true }).filter({ has: page.getByRole("status").filter({ hasText: new RegExp(`^${state}$`) }) });
}

async function decision(page: Page, amount: string) {
  await openScreen(page, "approvals");
  const offer = page.getByRole("heading", { name: "Supplier B", exact: true }).locator("..");
  await expect(offer.getByText("Decision total", { exact: true }).locator("..").getByText(amount, { exact: true })).toBeVisible();
}

test("owner reviews real evidence, rejects usage, accepts and replaces a fee rule, then revokes it", async ({ page, demo }, testInfo) => {
  prepare(demo.run, "private");
  await signIn(page, demo, "owner-a", "rules");
  const learned = page.getByRole("region", { name: "Learned rules", exact: true });
  await learned.getByRole("button", { name: "Generate rule proposals", exact: true }).click();
  const fee = rule(page, "Observed delivery fee", "Proposed · not active");
  await expect(fee.getByRole("button", { name: "Accept rule", exact: true })).toBeDisabled();
  await fee.getByRole("button", { name: "Source 1", exact: true }).click();
  await expect(fee.getByRole("region", { name: "Source viewer", exact: true }).getByText(/Delivery fee MYR10.00/)).toBeVisible();
  const usage = rule(page, "Usage rule", "Proposed · not active");
  await usage.getByRole("button", { name: "Reject proposal", exact: true }).click();
  await expect(rule(page, "Usage rule", "Rejected · not active")).toBeVisible();
  await decision(page, "RM 300.00");
  await page.getByRole("heading", { name: "Supplier B", exact: true }).locator("..").getByRole("button", { name: "Review solo order", exact: true }).click();
  await approve(page, "300.00");
  expect((await ownOrder(page)).status()).toBe(404);
  await openScreen(page, "rules");
  await fee.getByRole("checkbox").check();
  await fee.getByRole("button", { name: "Accept rule", exact: true }).click();
  await expect(rule(page, "Observed delivery fee", "Accepted · active")).toBeVisible();
  await decision(page, "RM 315.00");
  await expect(page.getByText("Your previous approval is void. Fresh consent is required.", { exact: true })).toBeVisible();
  expect((await ownOrder(page)).status()).toBe(404);
  await openScreen(page, "rules");
  await learned.screenshot({ path: testInfo.outputPath("owner-accepted-fee.png") });
  prepare(demo.run, "private-more");
  await learned.getByRole("button", { name: "Generate rule proposals", exact: true }).click();
  const replacement = rule(page, "Observed delivery fee", "Proposed · not active");
  await expect(replacement.getByText("Mean invoiced delivery fee: RM 20.00", { exact: true })).toBeVisible();
  await replacement.getByRole("checkbox").check();
  await replacement.getByRole("button", { name: "Accept rule", exact: true }).click();
  await expect(rule(page, "Observed delivery fee", "Superseded · not active")).toBeVisible();
  const active = rule(page, "Observed delivery fee", "Accepted · active");
  await active.getByRole("button", { name: "Revoke accepted rule", exact: true }).click();
  await expect(rule(page, "Observed delivery fee", "Revoked · not active")).toBeVisible();
  await decision(page, "RM 300.00");
  await page.reload();
  await expect(page.getByText("Your previous approval is void. Fresh consent is required.", { exact: true })).toBeVisible();
  await openScreen(page, "rules");
  await expect(rule(page, "Observed delivery fee", "Revoked · not active")).toBeVisible();
  await page.goto("/ms/rules");
  await expect(page.getByRole("heading", { name: "Peraturan dipelajari", exact: true, level: 1 })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: /^Dibatalkan · tidak aktif$/ })).toBeVisible();
  await page.goto("/zh/rules");
  await expect(page.getByRole("heading", { name: "学习规则", exact: true, level: 1 })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: /^已撤销 · 未生效$/ })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await signIn(page, demo, "staff", "rules");
  const hidden = page.getByRole("region", { name: "Learned rules", exact: true });
  await expect(hidden.getByRole("article").first()).toBeVisible();
  expect(await hidden.innerText()).not.toContain("RM");
  await expect(hidden.getByRole("button", { name: /Accept rule|Reject proposal|Revoke accepted rule|Generate rule proposals|Source \d/ })).toHaveCount(0);
  const { business } = await ownNeed(page);
  const response = await page.request.get(`/api/v1/businesses/${business}/rules`);
  const saved = (await response.json()).items[0];
  const denied = await page.request.post(`/api/v1/businesses/${business}/rules/${saved.id}/accept`, { headers: { Origin: "http://127.0.0.1:3000" }, data: { expected_revision: saved.revision } });
  expect(denied.status()).toBe(403);
});

test("accepted measured usage replaces the stated rate only until explicit revocation", async ({ page, demo }) => {
  prepare(demo.run, "private");
  await signIn(page, demo, "owner-a", "rules");
  const learned = page.getByRole("region", { name: "Learned rules", exact: true });
  await learned.getByRole("button", { name: "Generate rule proposals", exact: true }).click();
  const usage = rule(page, "Usage rule", "Proposed · not active");
  await expect(usage.getByText("Conservative daily usage: 50 pieces/day", { exact: true })).toBeVisible();
  await usage.getByRole("checkbox").check();
  await usage.getByRole("button", { name: "Accept rule", exact: true }).click();
  await expect(rule(page, "Usage rule", "Accepted · active")).toBeVisible();
  await openScreen(page, "approvals");
  await expect(page.getByText(/50 pieces\/day · Accepted learned usage/)).toBeVisible();
  await openScreen(page, "rules");
  await rule(page, "Usage rule", "Accepted · active").getByRole("button", { name: "Revoke accepted rule", exact: true }).click();
  await openScreen(page, "approvals");
  await expect(page.getByText(/125 pieces\/day · Owner-stated usage/)).toBeVisible();
  expect((await ownOrder(page)).status()).toBe(404);
});

test("network admin sees the three-business empty state then reviews five-business history without private evidence", async ({ page, demo }, testInfo) => {
  await signIn(page, demo, "admin");
  const learned = page.getByRole("region", { name: "Network learned rules", exact: true });
  await learned.getByRole("button", { name: "Generate rule proposals", exact: true }).click();
  await expect(learned.getByText("No qualifying historical aggregate is available. No network rule is active from this result.", { exact: true })).toBeVisible();
  await expect(learned.getByRole("button", { name: "Accept rule", exact: true })).toHaveCount(0);
  const evidence = prepare(demo.run, "network");
  await learned.getByRole("button", { name: "Generate rule proposals", exact: true }).click();
  const fee = rule(page, "Observed delivery fee", "Proposed · not active");
  await expect(fee.getByText("Mean invoiced delivery fee: RM 15.00", { exact: true })).toBeVisible();
  await expect(fee.getByText("15", { exact: true })).toBeVisible();
  await expect(fee.getByText("5", { exact: true })).toBeVisible();
  await expect(learned.getByRole("button", { name: /^Source \d/ })).toHaveCount(0);
  await fee.getByRole("checkbox").check();
  await fee.getByRole("button", { name: "Accept rule", exact: true }).click();
  await expect(rule(page, "Observed delivery fee", "Accepted · active")).toBeVisible();
  const who = await (await page.request.get("/api/v1/me")).json();
  const response = await page.request.get(`/api/v1/networks/${who.networks[0].network_id}/rules`);
  const body = await response.text();
  expect(response.headers()["cache-control"]).toContain("no-store");
  for (const identity of evidence.private_ids) expect(body).not.toContain(identity);
  expect(body).not.toContain('"evidence"');
  const current = JSON.parse(body).items[0];
  const crossOrigin = await page.request.post(`/api/v1/networks/${who.networks[0].network_id}/rules/${current.id}/revoke`, { headers: { Origin: "https://untrusted.example" }, data: { expected_revision: current.revision } });
  expect(crossOrigin.status()).toBe(403);
  await expect(learned.getByRole("button", { name: "Generate rule proposals", exact: true })).toBeEnabled();
  await learned.screenshot({ path: testInfo.outputPath("network-aggregate.png") });
  await page.reload();
  await expect(rule(page, "Observed delivery fee", "Accepted · active")).toBeVisible();
  await rule(page, "Observed delivery fee", "Accepted · active").getByRole("button", { name: "Revoke accepted rule", exact: true }).click();
  await expect(rule(page, "Observed delivery fee", "Revoked · not active")).toBeVisible();
  const denied = await page.request.get(`/api/v1/businesses/${evidence.business_id}/rules`);
  expect(denied.status()).toBe(404);
});
