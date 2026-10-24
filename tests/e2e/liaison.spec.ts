import { test, expect, signIn, ownNeed } from "./fixtures";

test("reviewed supplier text becomes a quote only through Gate 1", async ({ page, demo }) => {
  await signIn(page, demo, "owner-a");
  await page.getByLabel("Source file", { exact: true }).setInputFiles("fixtures/sources/quote-missing-delivery.txt");
  const uploaded = page.waitForResponse((response) => response.url().endsWith("/sources") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Upload source", exact: true }).click();
  const source = (await (await uploaded).json()).source;
  const viewer = page.getByRole("region", { name: "Source viewer", exact: true });
  await expect(viewer.getByText("SYNTHETIC. 250ml paper cups: RM0.30/piece, minimum 1000. Delivery and tax treatment to be confirmed.", { exact: true })).toBeVisible();
  await viewer.getByRole("checkbox").check();
  await viewer.getByRole("button", { name: "Prepare reviewed source", exact: true }).click();
  await expect(viewer.getByText("Source prepared. Extraction is queued; no record has been confirmed.", { exact: true })).toBeVisible();

  const { business, need } = await ownNeed(page);
  const root = `/api/v1/businesses/${business}`;
  const comparison = await (await page.request.get(`${root}/needs/${need}/comparisons`)).json();
  const offer = comparison.offers[0];
  const headers = { Origin: new URL(page.url()).origin, "Idempotency-Key": crypto.randomUUID() };
  const requested = await page.request.post(`${root}/quote-proposals`, { headers, data: {
    source_id: source.id, source_sha256: source.sha256, supplier_id: offer.supplier_id,
    spec_id: comparison.need.spec_id, spec_revision: comparison.need.spec_revision,
  } });
  expect(requested.status()).toBe(200);
  const job = await requested.json();
  await expect.poll(async () => (await (await page.request.get(`${root}/jobs/${job.id}`)).json()).status).toBe("succeeded");
  const completed = await (await page.request.get(`${root}/jobs/${job.id}`)).json();
  const path = `${root}/proposals/${completed.result_reference}`;
  const proposal = await (await page.request.get(path)).json();
  expect(proposal.status).toBe("pending_review");
  expect(proposal.record_id).toBeNull();
  expect(proposal.interpretation_mode).toBe("replay");
  expect(proposal.reading.delivery_fee).toBeNull();
  expect(proposal.reading.tax_rate).toBeNull();
  const edited = await page.request.patch(path, { headers: { ...headers, "Idempotency-Key": crypto.randomUUID() }, data: {
    expected_revision: 1, reading: { ...proposal.reading, pack_multiple: "1" },
  } });
  expect(edited.status()).toBe(200);
  const revision = await edited.json();
  const confirmed = await page.request.post(`${path}/confirm`, { headers: { ...headers, "Idempotency-Key": crypto.randomUUID() }, data: {
    expected_revision: 2, acknowledged_flags: revision.flags,
    quote_id: offer.quote_id, expected_quote_revision: offer.quote_revision,
  } });
  expect(confirmed.status()).toBe(200);
  expect((await confirmed.json()).status).toBe("confirmed");
  const current = await (await page.request.get(`${root}/needs/${need}/comparisons`)).json();
  const updated = current.offers.find((value: { quote_id: string }) => value.quote_id === offer.quote_id);
  expect(updated.quote_revision).toBe(2);
  expect(updated.missing_fields).toContain("delivery_fee");
  expect(updated.cost.decision_cost).toBeNull();
  expect((await page.request.get(`${root}/needs/${need}/order`)).status()).toBe(404);
});
