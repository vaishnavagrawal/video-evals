// Real-API checks (NEXT_PUBLIC_VREVIEW_MOCK=0, vreview-api running). Does NOT create a review run.
//  1. no Next.js dev-overlay issue on /, /reviews/new and a review page
//  2. Accept / False positive / Comment are saved through the API and survive a reload
//  3. Cancel during an upload calls POST /v1/uploads/{asset_id}/abort (200) and resets the form
// Usage: RUN=<succeeded run id> BIG=<large video file> node scripts/real-checks.mjs [baseUrl]
import { chromium } from "playwright-core";
import { mkdir } from "node:fs/promises";

const base = process.argv[2] ?? "http://localhost:53000";
const out = new URL("../screenshots/", import.meta.url).pathname;
const run = process.env.RUN;
const big = process.env.BIG;
if (!run) throw new Error("set RUN to a succeeded run id (see the dashboard)");
await mkdir(out, { recursive: true });

const browser = await chromium.launch({ executablePath: process.env.CHROME ?? "/usr/bin/google-chrome" });
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
const problems = [];
const apiCalls = [];
page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
page.on("response", (r) => {
  const u = r.url();
  if (u.includes("/v1/") && r.request().method() !== "OPTIONS") apiCalls.push(`${r.request().method()} ${r.status()} ${u.split("?")[0].replace(/^https?:\/\/[^/]+/, "")}`);
});

// Reads the dev overlay without screenshots (screenshots would need caret: "initial", see screenshots.mjs).
const overlay = () =>
  page.evaluate(async () => {
    const root = document.querySelector("nextjs-portal")?.shadowRoot;
    const badge = [...(root?.querySelectorAll("[data-issues]") ?? [])].map((e) => e.innerText.trim().replace(/\s+/g, " ")).filter(Boolean);
    if (!badge.length) return "";
    root.querySelector("[data-issues-open]")?.click();
    await new Promise((r) => setTimeout(r, 600));
    return badge.join(" ") + " :: " + (root.querySelector("[role=dialog]")?.innerText ?? "").replace(/\s+/g, " ").slice(0, 800);
  });
const checkOverlay = async (label) => {
  const o = await overlay();
  console.log(`overlay ${label}:`, o || "none");
  if (o) problems.push(`dev overlay on ${label}: ${o}`);
};

await page.goto(`${base}/login`);
await page.getByLabel("Email").fill(process.env.TEST_EMAIL ?? "test@vreview.local");
await page.getByLabel("Password").fill(process.env.TEST_PASSWORD ?? "test-password-123");
await page.locator("button[type=submit]").click();
await page.waitForURL(`${base}/`);
await page.getByRole("table").waitFor();
await page.waitForTimeout(1500);
await checkOverlay("/");

await page.goto(`${base}/reviews/new`);
await page.getByRole("heading", { name: "New review" }).waitFor();
await page.waitForTimeout(1500);
await checkOverlay("/reviews/new");

// --- feedback persistence
await page.goto(`${base}/reviews/${run}`);
await page.getByText("Summary").first().waitFor();
await page.waitForTimeout(1500);
await checkOverlay(`/reviews/${run}`);
const first = page.locator("li[id^=thread-]").first();
await first.getByRole("button", { name: /^(?!Seek)/ }).first().click(); // expand
const stamp = `check ${new Date().toISOString()}`;
await first.getByPlaceholder(/Leave a comment/).fill(stamp);
await first.getByRole("button", { name: "Comment", exact: true }).click();
await first.locator("li", { hasText: "You:" }).filter({ hasText: stamp }).waitFor();
await first.getByRole("button", { name: /False positive/ }).last().click();
await first.getByText("False positive").first().waitFor();
await page.waitForFunction(() => !document.body.innerText.includes("Saving"), null, { timeout: 10000 });
console.log("feedback POSTs:", apiCalls.filter((c) => c.includes("/feedback") && c.startsWith("POST")).join(", "));
await page.reload();
await page.getByText("Summary").first().waitFor();
const first2 = page.locator("li[id^=thread-]").first();
await first2.getByRole("button", { name: /^(?!Seek)/ }).first().click();
const persisted = {
  verdict: await first2.getByText("False positive").first().isVisible().catch(() => false),
  comment: await first2.locator("li", { hasText: "You:" }).filter({ hasText: stamp }).isVisible().catch(() => false),
};
console.log("after reload:", JSON.stringify(persisted), "| GET", apiCalls.filter((c) => c.startsWith("GET") && c.includes("/feedback")).slice(-1)[0]);
if (!persisted.verdict || !persisted.comment) problems.push(`feedback did not survive reload: ${JSON.stringify(persisted)}`);
// leave the run accepted
await first2.getByRole("button", { name: /Accept/ }).last().click();
await first2.getByText("Accepted").first().waitFor();
await page.waitForFunction(() => !document.body.innerText.includes("Saving"), null, { timeout: 10000 });

// --- cancel -> abort
if (big) {
  apiCalls.length = 0;
  await page.goto(`${base}/reviews/new`);
  await page.locator('input[type="file"]').nth(0).setInputFiles(big);
  await page.getByRole("button", { name: "Upload and start review" }).click();
  await page.getByText(/Uploading… [1-9]/).first().waitFor({ timeout: 60000 });
  await page.getByRole("button", { name: "Cancel" }).click();
  await page.getByRole("button", { name: "Upload and start review" }).waitFor();
  await page.waitForTimeout(2500); // late events from the aborted upload would land here
  const aborts = apiCalls.filter((c) => /POST \d+ \/v1\/uploads\/[^/]+\/abort$/.test(c));
  const after = {
    progressbars: await page.getByRole("progressbar").count(),
    stale: await page.getByText(/Uploading…|Cancelled|Upload failed/).count(),
  };
  console.log("abort calls:", aborts.join(", ") || "none", "| after cancel:", JSON.stringify(after));
  if (aborts.length < 1 || !aborts.every((a) => a.startsWith("POST 200"))) problems.push(`abort calls: ${aborts.join(", ") || "none"}`);
  if (after.progressbars || after.stale) problems.push(`form not reset after cancel: ${JSON.stringify(after)}`);
  await checkOverlay("/reviews/new after cancel");
} else console.log("BIG not set: skipping the cancel/abort check");

await browser.close();
if (problems.length) {
  console.log("PROBLEMS:\n" + problems.join("\n"));
  process.exit(1);
}
console.log("OK");
