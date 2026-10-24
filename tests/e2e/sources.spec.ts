import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { test, expect, signIn } from "./fixtures";

test("owner reviews a real invoice crop and masks a private provider copy", async ({ page, demo }, testInfo) => {
  await signIn(page, demo, "owner-a", "inbox");
  await page.getByLabel("Source file", { exact: true }).setInputFiles("fixtures/sources/delivered-invoice.png");
  await page.getByRole("button", { name: "Upload source", exact: true }).click();
  const viewer = page.getByRole("region", { name: "Source viewer", exact: true });
  const image = viewer.getByRole("img", { name: "Private source page", exact: true });
  await expect(image).toBeVisible();
  await image.evaluate((element) => (element as HTMLImageElement).decode());
  const truth = JSON.parse(readFileSync("fixtures/gold/source-fields.json", "utf8"));
  const bounds = truth["delivered-invoice"].bbox_normalized["lines.0.unit_price"] as number[];
  await viewer.getByRole("button", { name: "Add mask region", exact: true }).click();
  await image.scrollIntoViewIfNeeded();
  let box = await image.boundingBox();
  if (!box) throw new Error("Source page is unavailable");
  await page.mouse.move(box.x + bounds[0] * box.width / 1000, box.y + bounds[1] * box.height / 1000);
  await page.mouse.down();
  await page.mouse.move(box.x + bounds[2] * box.width / 1000, box.y + bounds[3] * box.height / 1000, { steps: 3 });
  await page.mouse.up();
  await viewer.getByRole("button", { name: "Inspect selected region 1", exact: true }).click();
  const crop = viewer.getByRole("img", { name: "Source crop", exact: true });
  await expect(crop).toBeVisible();
  await crop.evaluate((element) => (element as HTMLImageElement).decode());
  const cropBounds = new URL((await crop.getAttribute("src"))!, "http://127.0.0.1:3000").searchParams.getAll("bbox").map(Number);
  cropBounds.forEach((value, index) => expect(Math.abs(value - bounds[index])).toBeLessThan(8));
  await crop.screenshot({ path: testInfo.outputPath("invoice-price-crop.png") });
  await viewer.getByRole("button", { name: "Clear masks", exact: true }).click();
  await viewer.getByRole("button", { name: "Add mask region", exact: true }).click();
  await image.scrollIntoViewIfNeeded();
  box = await image.boundingBox();
  if (!box) throw new Error("Source page is unavailable");
  await page.mouse.move(box.x + 5, box.y + 5);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 5, box.y + box.height / 12, { steps: 3 });
  await page.mouse.up();
  await viewer.getByRole("checkbox").check();
  await viewer.getByRole("button", { name: "Prepare reviewed source", exact: true }).click();
  await expect(viewer.getByText("Source prepared. Extraction is queued; no record has been confirmed.", { exact: true })).toBeVisible();
  await image.evaluate((element) => (element as HTMLImageElement).decode());
  await image.screenshot({ path: testInfo.outputPath("prepared-invoice-page.png") });
  await signIn(page, demo, "staff", "inbox");
  await page.getByRole("button", { name: "View source", exact: true }).first().click();
  await expect(viewer.getByText("This account cannot view price-bearing sources.", { exact: true })).toBeVisible();
  await expect(viewer.getByRole("img")).toHaveCount(0);
});

test("PDF and rotated photo pages render in the source viewer", async ({ page, demo }, testInfo) => {
  const rotated = testInfo.outputPath("rotated-source.jpg");
  execFileSync("uv", ["run", "--project", "services/api", "python", "-c", `
import sys
from PIL import Image, ImageDraw
image=Image.new('RGB',(240,400),'white')
ImageDraw.Draw(image).rectangle((0,0,239,79),fill='red')
exif=Image.Exif();exif[274]=6
image.save(sys.argv[1],format='JPEG',exif=exif)
`, rotated], { stdio: "pipe" });
  await signIn(page, demo, "owner-a", "inbox");
  for (const file of ["fixtures/sources/delivered-invoice.pdf", rotated]) {
    await page.getByLabel("Source file", { exact: true }).setInputFiles(file);
    const uploaded = page.waitForResponse((response) => response.url().endsWith("/sources") && response.request().method() === "POST");
    await page.getByRole("button", { name: "Upload source", exact: true }).click();
    const result = await (await uploaded).json();
    const viewer = page.getByRole("region", { name: "Source viewer", exact: true });
    await expect(viewer.getByText(`Source reference: ${result.source.id}`, { exact: true })).toBeVisible();
    const image = viewer.getByRole("img", { name: "Private source page", exact: true });
    await expect(image).toBeVisible();
    await image.evaluate((element) => (element as HTMLImageElement).decode());
    if (file === rotated) {
      const dimensions = await image.evaluate((element) => ({ width: (element as HTMLImageElement).naturalWidth, height: (element as HTMLImageElement).naturalHeight }));
      expect(dimensions).toEqual({ width: 400, height: 240 });
    }
    await image.screenshot({ path: testInfo.outputPath(file === rotated ? "rotated-page.png" : "pdf-page.png") });
  }
});
