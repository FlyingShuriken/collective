import { execFileSync } from "node:child_process";
import { test, expect, signIn } from "./fixtures";

test("background retries and failures remain separate from confirmation", async ({ page, demo }, testInfo) => {
  execFileSync("uv", ["run", "--project", "services/api", "python", "-c", `
import asyncio,json,sys
from pathlib import Path
from datetime import UTC,datetime
from uuid import NAMESPACE_URL,UUID,uuid4,uuid5
from collective.settings import Settings
from collective.repositories.db import Database
from collective.repositories import jobs

async def seed():
    fixture=json.loads(Path('fixtures/scenarios/three-cafes.json').read_text())
    business=uuid5(NAMESPACE_URL,'orbyte-run:'+sys.argv[1]+':'+str(UUID(fixture['businesses'][0]['id'])))
    db=Database(Settings().database_url.get_secret_value())
    await db.open()
    try:
        for retry in (False,True):
            kind='ui-job-'+uuid4().hex
            now=datetime.now(UTC)
            async with db.transaction('coordinator',None) as conn:
                await jobs.enqueue(conn,business=business,kind=kind,revision=1,key=uuid4().hex,
                    payload={'private':'PRIVATE_VALUE_MUST_NOT_APPEAR'},due=now)
                claim=await jobs.claim(conn,uuid4(),(kind,),now)
                await jobs.fail(conn,claim,now,retryable=retry,code='RETRYABLE_FAILURE' if retry else 'JOB_FAILED')
    finally:
        await db.close()
asyncio.run(seed())
`, demo.run], { stdio: "pipe" });
  await signIn(page, demo, "owner-a");
  const jobs = page.getByRole("region", { name: "Background tasks", exact: true });
  await expect(jobs.getByText("Retry scheduled", { exact: true })).toBeVisible();
  await expect(jobs.getByText("Needs review", { exact: true })).toBeVisible();
  await expect(jobs.getByText("Completing a task does not approve a record, rule or order.", { exact: true })).toBeVisible();
  await expect(page.locator("body")).not.toContainText("PRIVATE_VALUE_MUST_NOT_APPEAR");
  await jobs.screenshot({ path: testInfo.outputPath("background-task-status.png") });
  await signIn(page, demo, "staff");
  await expect(jobs.getByText("Retry scheduled", { exact: true })).toBeVisible();
  await expect(page.locator("body")).not.toContainText(/RM\s*[0-9]/);
});
