import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { test, expect, signIn, ownNeed } from "./fixtures";

test("fresh synthetic RFQ replay stays a draft and an unseen source fails visibly", async ({ page, demo }) => {
  await signIn(page, demo, "owner-a");
  const { business, need } = await ownNeed(page);
  const root = `/api/v1/businesses/${business}`;
  const comparison = await (await page.request.get(`${root}/needs/${need}/comparisons`)).json();
  const supplier = comparison.offers[0].supplier_id;
  const preload = ["run", "--project", "services/api", "python", "scripts/preload_query_replay.py",
    "--run", demo.run, "--need-id", need, "--supplier-id", supplier];
  const manifestBefore = readFileSync("fixtures/replay/manifest.json", "utf8");
  const authored = JSON.parse(execFileSync("uv", preload, { stdio: "pipe" }).toString());
  const headers = () => ({ Origin: new URL(page.url()).origin, "Idempotency-Key": crypto.randomUUID() });
  try {
    const requested = await page.request.post(`${root}/drafts`, { headers: headers(),
      data: { kind: "rfq", reference_id: need, expected_revision: 1, supplier_id: supplier } });
    expect(requested.status()).toBe(200);
    const job = await requested.json();
    await expect.poll(async () => (await (await page.request.get(`${root}/jobs/${job.id}`)).json()).status).toBe("succeeded");
    const completed = await (await page.request.get(`${root}/jobs/${job.id}`)).json();
    const draft = await (await page.request.get(`${root}/drafts/${completed.result_reference}`)).json();
    expect(draft.status).toBe("draft");
    expect(draft.text_content).toContain("1000");
    expect(draft.status).not.toBe("sent_simulated");
  } finally {
    execFileSync("uv", [...preload, "--clean", "--source-hash", authored.source_hash], { stdio: "pipe" });
    expect(readFileSync("fixtures/replay/manifest.json", "utf8")).toBe(manifestBefore);
  }
  await page.getByRole("navigation", { name: "Workspace navigation", exact: true }).getByRole("link", { name: "Inbox", exact: true }).click();
  await page.getByLabel("Or paste invoice, stock or supplier text", { exact: true }).fill(`SYNTHETIC unseen source ${demo.run}: delivery RM17.37; requires human review.`);
  const upload = page.waitForResponse((response) => response.url().endsWith("/sources") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Upload source", exact: true }).click();
  const source = (await (await upload).json()).source;
  const viewer = page.getByRole("region", { name: "Source viewer", exact: true });
  await viewer.getByRole("checkbox").check();
  await viewer.getByRole("button", { name: "Prepare reviewed source", exact: true }).click();
  await expect.poll(async () => {
    const jobs = await (await page.request.get(`${root}/jobs`)).json();
    return jobs.items.find((item: { kind: string; error_code: string }) => item.kind === "intake" && item.error_code === "REPLAY_MISS")?.status;
  }).toBe("failed");
  const proposals = await (await page.request.get(`${root}/proposals?source_id=${source.id}`)).json();
  expect(proposals.items).toHaveLength(0);
  await expect(page.getByRole("region", { name: "Source reading", exact: true }).getByRole("status")).toContainText("No exact replay is available");
});

test("a human-corrected stock quantity changes core replenishment instead of replaying a decision", async ({ page, demo }) => {
  await signIn(page, demo, "owner-a", "inbox");
  const { business, need } = await ownNeed(page);
  const root = `/api/v1/businesses/${business}`;
  const before = await (await page.request.get(`${root}/needs/${need}/options`)).json();
  await page.getByLabel("Source file", { exact: true }).setInputFiles("fixtures/sources/stock-ms.txt");
  const uploaded = page.waitForResponse((response) => response.url().endsWith("/sources") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Upload source", exact: true }).click();
  const source = (await (await uploaded).json()).source;
  const viewer = page.getByRole("region", { name: "Source viewer", exact: true });
  await viewer.getByRole("checkbox").check();
  await viewer.getByRole("button", { name: "Prepare reviewed source", exact: true }).click();
  await expect(page.getByRole("region", { name: "Record review", exact: true })).toBeVisible();
  const proposal = (await (await page.request.get(`${root}/proposals?source_id=${source.id}`)).json()).items[0];
  expect(proposal.reading.stock_quantity).toBe("750");
  const mappings = await (await page.request.get(`${root}/mappings`)).json();
  const mapping = mappings.items.find((item: { confirmed: boolean; source_unit: string }) => item.confirmed && item.source_unit === "piece") ?? mappings.items.find((item: { confirmed: boolean }) => item.confirmed);
  const reading = { ...proposal.reading, stock_quantity: "100", stock_observed_at: new Date().toISOString() };
  const context = { ...proposal.context, stock_mapping: { id: mapping.id, revision: mapping.revision } };
  const headers = () => ({ Origin: new URL(page.url()).origin, "Idempotency-Key": crypto.randomUUID() });
  const edited = await page.request.patch(`${root}/proposals/${proposal.id}`, { headers: headers(), data: { expected_revision: 1, reading, context } });
  expect(edited.status()).toBe(200);
  const revision = await edited.json();
  const confirmed = await page.request.post(`${root}/proposals/${proposal.id}/confirm`, { headers: headers(), data: { expected_revision: 2, acknowledged_flags: revision.flags } });
  expect(confirmed.status()).toBe(200);
  const after = await (await page.request.get(`${root}/needs/${need}/options`)).json();
  expect(Number(after.replenishment.stock_at_now)).toBeLessThanOrEqual(100);
  expect(after.replenishment.stock_at_now).not.toBe(before.replenishment.stock_at_now);
  expect(new Date(after.replenishment.run_out_at).getTime()).toBeLessThan(new Date(before.replenishment.run_out_at).getTime());
  expect((await page.request.get(`${root}/needs/${need}/order`)).status()).toBe(404);
});
