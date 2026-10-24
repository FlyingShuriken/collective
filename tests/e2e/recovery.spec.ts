import { execFileSync } from "node:child_process";
import { test, expect, signIn, ownNeed, ownOrder, approve } from "./fixtures";

test("killed worker and API recover one draft; repeated exact Send creates one receipt", async ({ page, demo }) => {
  test.setTimeout(90_000);
  await signIn(page, demo, "owner-a");
  const { business, need } = await ownNeed(page);
  const root = `/api/v1/businesses/${business}`;
  const comparison = await (await page.request.get(`${root}/needs/${need}/comparisons`)).json();
  const recovered = JSON.parse(execFileSync("uv", ["run", "--project", "services/api", "python",
    "scripts/rehearse_recovery.py", "--run", demo.run, "--supplier-id", comparison.offers[0].supplier_id],
    { stdio: "pipe", timeout: 60_000 }).toString());
  expect(recovered.attempts).toBe(2);
  expect(recovered.results).toBe(1);
  const draft = await (await page.request.get(`${root}/drafts/${recovered.draft_id}`)).json();
  expect(draft.status).toBe("draft");
  const headers = { Origin: new URL(page.url()).origin, "Idempotency-Key": crypto.randomUUID() };
  const data = { expected_revision: draft.revision, approved_hash: draft.approved_hash, recipient: draft.recipient };
  const send = () => page.request.post(`${root}/drafts/${draft.id}/send`, { headers, data });
  const first = await send();
  expect(first.status()).toBe(200);
  const receipt = await first.json();
  expect(receipt.simulated).toBe(true);
  await page.reload();
  expect(await (await send()).json()).toEqual(receipt);
  expect((await (await page.request.get(`${root}/drafts/${draft.id}`)).json()).status).toBe("sent_simulated");
});

test("retrying an exact release after a page restart returns the same simulated PO", async ({ page, demo }) => {
  await signIn(page, demo, "owner-a");
  await page.getByRole("button", { name: "Review solo order", exact: true }).first().click();
  await approve(page, "315.00");
  const request = page.waitForRequest((value) => value.url().endsWith("/release") && value.method() === "POST");
  await page.getByRole("button", { name: "Create simulated PO", exact: true }).click();
  const released = await request;
  await expect(page.getByRole("region", { name: "Simulated purchase order", exact: true }).getByText("Order issued", { exact: true })).toBeVisible();
  const first = await (await ownOrder(page)).json();
  await page.reload();
  const retried = await page.request.post(released.url(), { headers: { ...released.headers(), Origin: new URL(page.url()).origin }, data: released.postDataJSON() });
  expect(retried.status()).toBe(200);
  expect((await retried.json()).order.id).toBe(first.id);
  expect(await (await ownOrder(page)).json()).toEqual(first);
});
