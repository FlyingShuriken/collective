import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { test as base, expect, type Page } from "@playwright/test";

type Demo = {
  run: string;
  anchor: string;
  accounts: Record<string, { id: string; email: string; password: string }>;
};

export const test = base.extend<{ demo: Demo }>({
  demo: async ({}, use) => {
    const run = `e2e-${randomUUID().slice(0, 12)}`;
    execFileSync("uv", ["run", "--project", "services/api", "python", "scripts/seed_demo.py",
      "--scenario", "three-cafes", "--run", run, "--anchor", "now"], { stdio: "pipe" });
    await use(JSON.parse(readFileSync(`.cache/demo-${run}.json`, "utf8")) as Demo);
  },
});
export { expect };

export async function openScreen(page: Page, screen: "inbox" | "approvals" | "ledger" | "rules" | "activity") {
  const names = { inbox: "Inbox", approvals: "Your next order", ledger: "Ledger", rules: "Learned rules", activity: "Activity" };
  await page.getByRole("navigation", { name: "Workspace navigation", exact: true }).getByRole("link", { name: names[screen], exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/en/${screen}(?:\\?|$)`));
}

export async function signIn(page: Page, demo: Demo, account: string, screen?: "inbox" | "approvals" | "ledger" | "rules" | "activity") {
  await page.context().clearCookies();
  await page.goto("/en/sign-in");
  await page.getByLabel("Email", { exact: true }).fill(demo.accounts[account].email);
  await page.getByLabel("Password", { exact: true }).fill(demo.accounts[account].password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.getByRole("link", { name: "Open workspace", exact: true }).click();
  await expect(page.getByRole("heading", { name: account === "admin" ? "Network learned rules" : "250ml paper cup", exact: true })).toBeVisible();
  if (screen) await openScreen(page, screen);
}

export async function approve(page: Page, cap: string) {
  await page.getByLabel("Maximum delivered cost (RM)", { exact: true }).fill(cap);
  const expiry = new Date(Date.now() + 14 * 60 * 60 * 1000).toISOString().slice(0, 16);
  await page.getByLabel("Approval expiry (Malaysia time, UTC+8)", { exact: true }).fill(expiry);
  await page.getByRole("button", { name: /^(Approve these terms|Give fresh approval)$/ }).click();
  await expect(page.getByText("Your approval stands", { exact: true })).toBeVisible();
}

export async function ownNeed(page: Page) {
  const identity = await (await page.request.get("/api/v1/me")).json();
  const business = identity.memberships[0].business_id as string;
  const needs = await (await page.request.get(`/api/v1/businesses/${business}/needs`)).json();
  return { business, need: needs.items[0].id as string };
}

export async function ownOrder(page: Page) {
  const { business, need } = await ownNeed(page);
  return page.request.get(`/api/v1/businesses/${business}/needs/${need}/order`);
}

export async function readyPool(page: Page, demo: Demo) {
  await signIn(page, demo, "owner-a");
  await page.getByRole("button", { name: "Find a pool", exact: true }).click();
  await approve(page, "276.00");
  await signIn(page, demo, "owner-b");
  await approve(page, "285.00");
  await signIn(page, demo, "owner-c");
  await approve(page, "276.00");
  await expect(page.getByText("Ready for explicit order creation", { exact: true })).toBeVisible();
  expect((await ownOrder(page)).status()).toBe(404);
}
