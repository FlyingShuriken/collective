import { readFileSync, writeFileSync } from "node:fs";
import { test, expect, signIn } from "./fixtures";

test.use({ timezoneId: "America/Los_Angeles" });

test("three languages preserve MYR records, Malaysian dates and keyboard navigation at 320px", async ({ page, demo }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await signIn(page, demo, "owner-a", "ledger");
  const identity = await (await page.request.get("/api/v1/me")).json();
  const business = identity.memberships[0].business_id;
  const before = await (await page.request.get(`/api/v1/businesses/${business}/ledger`)).json();
  let current = "en";
  for (const locale of ["en", "ms", "zh"]) {
    const oldCopy = JSON.parse(readFileSync(`apps/web/src/messages/${current}.json`, "utf8"));
    const copy = JSON.parse(readFileSync(`apps/web/src/messages/${locale}.json`, "utf8"));
    await page.getByRole("navigation", { name: oldCopy.language, exact: true }).getByRole("link", { name: { en: "English", ms: "Bahasa Melayu", zh: "中文" }[locale], exact: true }).click();
    await expect(page.locator("html")).toHaveAttribute("lang", locale);
    await expect(page.getByRole("heading", { name: copy.screens.ledger, exact: true, level: 1 })).toBeVisible();
    const ledger = page.getByRole("region", { name: copy.ledger.title, exact: true });
    await expect(ledger).toContainText("RM 315.00");
    const expected = new Intl.DateTimeFormat({ en: "en-MY", ms: "ms-MY", zh: "zh-MY" }[locale], { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Kuala_Lumpur" }).format(new Date(before.items[0].recorded_at));
    await expect(ledger).toContainText(expected);
    const navigation = page.getByRole("navigation", { name: copy.screens.navigation, exact: true });
    await navigation.getByRole("link", { name: copy.screens.inbox, exact: true }).focus();
    await page.keyboard.press("Tab");
    await expect(navigation.getByRole("link", { name: copy.screens.approvals, exact: true })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(new RegExp(`/${locale}/approvals`));
    await navigation.getByRole("link", { name: copy.screens.inbox, exact: true }).click();
    await page.getByRole("button", { name: copy.uploadSource, exact: true }).click();
    await expect(page.getByRole("region", { name: copy.sources, exact: true }).getByRole("alert")).toContainText(copy.chooseSource);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`mobile-${locale}.png`), fullPage: true });
    await navigation.getByRole("link", { name: copy.screens.ledger, exact: true }).click();
    current = locale;
  }
  const after = await (await page.request.get(`/api/v1/businesses/${business}/ledger`)).json();
  expect(after).toEqual(before);
});

test("Chromium accepts install metadata and offline caching contains only public assets", async ({ playwright }, testInfo) => {
  const context = await playwright.chromium.launchPersistentContext(testInfo.outputPath("pwa-profile"), {
    headless: true, baseURL: "http://127.0.0.1:3000", viewport: { width: 390, height: 844 },
  });
  try {
    const page = context.pages()[0];
    await page.goto("/en/sign-in");
    await page.waitForFunction(() => !!navigator.serviceWorker.controller);
    const response = await page.request.get("/manifest.webmanifest");
    const manifest = await response.json();
    expect(manifest.name).toBe("Orbyte Collective");
    expect(manifest.display).toBe("standalone");
    expect(manifest.icons.map((icon: { sizes: string }) => icon.sizes)).toEqual(expect.arrayContaining(["192x192", "512x512"]));
    const client = await context.newCDPSession(page);
    await client.send("Page.getAppManifest");
    await expect.poll(async () => (await client.send("Page.getInstallabilityErrors")).installabilityErrors).toEqual([]);
    const worker = await page.request.get("/sw.js");
    expect(worker.headers()["cache-control"]).toContain("no-store");
    await page.evaluate(() => fetch("/api/v1/me", { cache: "no-store" }));
    const cached = await page.evaluate(async () => {
      const keys = await caches.keys();
      return (await Promise.all(keys.map(async (key) => (await (await caches.open(key)).keys()).map((request) => new URL(request.url).pathname)))).flat().sort();
    });
    expect(cached).toEqual(["/icons/icon-192.png", "/icons/icon-512.png", "/offline.html"]);
    await context.setOffline(true);
    expect(await page.evaluate(async () => {
      try { await fetch("/api/v1/me", { method: "POST" }); return false; }
      catch { return true; }
    })).toBe(true);
    await page.goto("/en/ledger");
    await expect(page.getByRole("heading", { name: "Connection unavailable", exact: true })).toBeVisible();
    await expect(page.locator("body")).not.toContainText(/RM\s*[0-9]|Café/);
    await expect(page.getByText("Reconnect to the Orbyte service to review your records. Changes are not queued offline.", { exact: true })).toBeVisible();
    writeFileSync(testInfo.outputPath("pwa-observation.json"), JSON.stringify({
      browser: context.browser()?.version(), installability_errors: [], cached_public_paths: cached,
      private_api_cached: false, offline_writes_queued: false, native_device_installation: "not_exercised",
    }, null, 2));
  } finally { await context.close(); }
});
