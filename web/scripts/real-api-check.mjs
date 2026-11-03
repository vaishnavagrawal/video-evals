// Real-API check (NEXT_PUBLIC_VREVIEW_MOCK=0, vreview-api running): sign in, screenshot the dashboard,
// upload a candidate + original + reference image through the UI, and land on the new review page.
// Usage: CANDIDATE=... ORIGINAL=... node scripts/real-api-check.mjs [baseUrl]
import { chromium } from "playwright-core";
import { mkdir } from "node:fs/promises";

const base = process.argv[2] ?? "http://localhost:53000";
const out = new URL("../screenshots/", import.meta.url).pathname;
const candidate = process.env.CANDIDATE;
const original = process.env.ORIGINAL;
const image = new URL("../public/mock/reference-frame.jpg", import.meta.url).pathname;
if (!candidate || !original) throw new Error("set CANDIDATE and ORIGINAL to small local video files");
await mkdir(out, { recursive: true });

const browser = await chromium.launch({ executablePath: process.env.CHROME ?? "/usr/bin/google-chrome" });
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
const errors = [];
const badge = () =>
  page.evaluate(() =>
    [...(document.querySelector("nextjs-portal")?.shadowRoot?.querySelectorAll("[data-issues]") ?? [])]
      .map((e) => e.innerText.trim().replace(/\s+/g, " "))
      .join(" "),
  );
const shot = async (name) => {
  await page.screenshot({ path: `${out}${name}.png`, caret: "initial" });
  const b = await badge();
  console.log("wrote", `screenshots/${name}.png`, b ? `DEV OVERLAY: ${b}` : "(no dev issues)");
};
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
page.on("console", (m) => m.type() === "error" && !m.text().includes("hydrat") && errors.push(`console: ${m.text().slice(0, 300)}`));
page.on("response", (r) => {
  const u = r.url();
  if (u.includes("/v1/") && r.request().method() !== "OPTIONS")
    console.log(r.request().method(), r.status(), u.split("?")[0].replace(/X-Amz.*/, ""));
});

await page.goto(`${base}/login`);
await page.getByLabel("Email").fill(process.env.TEST_EMAIL ?? "test@vreview.local");
await page.getByLabel("Password").fill(process.env.TEST_PASSWORD ?? "test-password-123");
await page.locator("button[type=submit]").click();
await page.waitForURL(`${base}/`);
await page.getByRole("heading", { name: "Reviews" }).waitFor();
await page.waitForTimeout(1000);
await shot("real-dashboard-before");

await page.goto(`${base}/reviews/new`);
await page.locator('input[type="file"]').nth(0).setInputFiles(candidate);
await page.locator('input[type="file"]').nth(0).setInputFiles([original, image]);
await page.getByLabel("Source language").selectOption("hi");
await page.getByLabel("Target language").selectOption("es");
await page.getByRole("button", { name: "Upload and start review" }).click();
await page.waitForURL(/\/reviews\/(?!new)/, { timeout: 120000 }).catch(async (e) => {
  await page.screenshot({ path: `${out}real-upload-failed.png` });
  throw e;
});
await page.waitForTimeout(1500);
await shot("real-review-after-upload");
console.log("review page:", page.url());

await page.goto(`${base}/`);
await page.getByRole("table").waitFor();
await page.waitForTimeout(800);
await shot("real-dashboard-after");

// Cancel a large upload: expect POST /uploads/{id}/abort (404 is ignored until the backend has it).
if (process.env.BIG) {
  await page.goto(`${base}/reviews/new`);
  await page.locator('input[type="file"]').nth(0).setInputFiles(process.env.BIG);
  await page.getByRole("button", { name: "Upload and start review" }).click();
  await page.getByText(/Uploading… [1-9]/).first().waitFor({ timeout: 30000 });
  await page.getByRole("button", { name: "Cancel" }).click();
  await page.getByRole("button", { name: "Upload and start review" }).waitFor();
  await page.waitForTimeout(1500);
  await shot("real-new-review-cancelled");
}
console.log(errors.length ? "browser errors:\n" + errors.join("\n") : "no browser errors");
await browser.close();
