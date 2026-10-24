import { execFileSync } from "node:child_process";
import { test, expect, signIn, openScreen } from "./fixtures";

test("switching from owner to restricted staff context clears private ledger values", async ({ page, demo }) => {
  const business = execFileSync("uv", ["run", "--project", "services/api", "python", "-c", `
import json,sys
from pathlib import Path
from uuid import UUID,NAMESPACE_URL,uuid5
import psycopg
from dotenv import dotenv_values
from collective.repositories.db import require_local_database
run=sys.argv[1]
assert run.startswith('e2e-')
scenario=json.loads(Path('fixtures/scenarios/three-cafes.json').read_text())
accounts=json.loads(Path('.cache/demo-'+run+'.json').read_text())['accounts']
business=uuid5(NAMESPACE_URL,f"orbyte-run:{run}:{UUID(scenario['businesses'][1]['id'])}")
with psycopg.connect(require_local_database(dotenv_values('.env')['MIGRATION_DATABASE_URL'])) as conn:
 conn.execute("insert into collective.business_memberships(business_id,user_id,role) values (%s,%s,'staff')",(business,accounts['owner-a']['id']))
print(business)
`, demo.run], { stdio: "pipe" }).toString().trim();
  await signIn(page, demo, "owner-a", "ledger");
  await expect(page.getByRole("region", { name: "Recorded ledger", exact: true })).toContainText("RM 315.00");
  await page.getByRole("combobox", { name: "Business", exact: true }).selectOption(business);
  await expect(page.getByRole("region", { name: "Recorded ledger", exact: true })).toContainText("Amounts and price-bearing source links are hidden for this account.");
  await expect(page.locator("body")).not.toContainText(/RM\s*[0-9]/);
  await expect(page.getByRole("button", { name: "View source", exact: true })).toHaveCount(0);
  const response = await page.request.get(`/api/v1/businesses/${business}/self-audit`);
  const audit = await response.json();
  expect(audit.price_visibility).toBe("hidden");
  expect(audit).not.toHaveProperty("confirmed_variance_amount");
  await openScreen(page, "inbox");
  await expect(page.getByRole("combobox", { name: "Business", exact: true })).toHaveValue(business);
  await signIn(page, demo, "admin");
  await expect(page.getByRole("navigation").getByRole("link", { name: "Inbox", exact: true })).toHaveCount(0);
  expect((await page.request.get(`/api/v1/businesses/${business}/self-audit`)).status()).toBe(404);
});
