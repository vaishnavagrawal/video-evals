// Screenshots of the main pages in mock mode (viewport shots only, each <= 2000 px; fails on dev-overlay issues) (run `pnpm dev` with NEXT_PUBLIC_VREVIEW_MOCK=1 first).
// Usage: node scripts/screenshots.mjs [baseUrl]. Signs in with the real better-auth (see verify-auth.mjs).
import { chromium } from "playwright-core";
import { mkdir, readFile } from "node:fs/promises";

const base = process.argv[2] ?? "http://localhost:53000";
const out = new URL("../screenshots/", import.meta.url).pathname;
const email = process.env.TEST_EMAIL ?? "test@vreview.local";
const password = process.env.TEST_PASSWORD ?? "test-password-123";
await mkdir(out, { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.CHROME ?? "/usr/bin/google-chrome",
  args: ["--autoplay-policy=no-user-gesture-required"],
});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
page.on("console", (m) => {
  if (m.type() === "error") errors.push(`console: ${m.text()}`);
  if (m.text().startsWith("[mock]")) console.log("browser:", m.text());
});
// Text of the Next.js dev-tools badge ("1 Issue" etc.); empty when there is nothing to report.
const badge = () =>
  page.evaluate(() =>
    [...(document.querySelector("nextjs-portal")?.shadowRoot?.querySelectorAll("[data-issues]") ?? [])]
      .map((e) => e.innerText.trim())
      .filter(Boolean)
      .join(" "),
  );
// Dev-overlay problems seen during the run; the script exits 1 if there are any.
const overlayIssues = [];
// Opens the overlay and returns its text (what the "N Issue" badge is about).
const overlayText = () =>
  page.evaluate(async () => {
    const root = document.querySelector("nextjs-portal")?.shadowRoot;
    root?.querySelector("[data-issues-open]")?.click();
    await new Promise((r) => setTimeout(r, 600));
    return (root?.querySelector("[role=dialog]")?.innerText ?? "").replace(/\s+/g, " ").slice(0, 700);
  });
const MAX_PX = 2000; // images above this are rejected by the model API when an agent views them
const pngSize = async (file) => {
  const b = await readFile(file); // IHDR: width at byte 16, height at byte 20 (big endian)
  return [b.readUInt32BE(16), b.readUInt32BE(20)];
};
// Viewport shots only: never fullPage, so no image can exceed MAX_PX in either dimension.
const shot = async (name, opts = {}) => {
  // caret: "initial" stops Playwright from injecting caret-color styles, which cause a hydration
  // mismatch if the shot is taken before React has hydrated the inputs.
  const file = `${out}${name}.png`;
  await page.screenshot({ path: file, caret: "initial", ...opts, fullPage: false });
  const [w, h] = await pngSize(file);
  if (w > MAX_PX || h > MAX_PX) throw new Error(`${name}.png is ${w}x${h}; keep screenshots <= ${MAX_PX}px`);
  const b = await badge();
  if (b) overlayIssues.push(`${name}: ${b} :: ${await overlayText()}`);
  console.log("wrote", `screenshots/${name}.png`, `${w}x${h}`, b ? `DEV OVERLAY: ${b}` : "(no dev issues)");
};
// The page must not scroll sideways: document and the main content fit the viewport width.
const noHorizontalOverflow = async (label) => {
  const m = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    doc: document.documentElement.scrollWidth,
    wide: [...document.querySelectorAll("body *")]
      .filter((e) => e.getBoundingClientRect().right > document.documentElement.clientWidth + 0.5)
      .slice(0, 3)
      .map((e) => `${e.tagName.toLowerCase()}.${String(e.className).slice(0, 40)} right=${Math.round(e.getBoundingClientRect().right)}`),
  }));
  const ok = m.doc <= m.viewport;
  console.log(`overflow ${label}: viewport ${m.viewport}, scrollWidth ${m.doc} -> ${ok ? "OK" : "OVERFLOW " + m.wide.join("; ")}`);
  if (!ok) overlayIssues.push(`horizontal overflow at ${label}: ${m.wide.join("; ")}`);
};

// Login
await page.goto(`${base}/`);
await page.waitForURL(/\/login/);
await shot("login");
await page.getByLabel("Email").fill(email);
await page.getByLabel("Password").fill(password);
await page.getByRole("button", { name: "Sign in", exact: true }).last().click();
await page.waitForURL(`${base}/`);

// Dashboard
await page.getByRole("table").waitFor();
await page.waitForTimeout(300);
await shot("dashboard-1440");
await noHorizontalOverflow("dashboard 1440");
await page.setViewportSize({ width: 1024, height: 768 });
await page.waitForTimeout(300);
await noHorizontalOverflow("dashboard 1024");
await shot("dashboard-1024");
await page.setViewportSize({ width: 1440, height: 900 });

// New review
await page.goto(`${base}/reviews/new`);
await page.getByRole("heading", { name: "New review" }).waitFor();
await shot("new-review-empty");
const mb = 1024 * 1024;
const inputs = page.locator('input[type="file"]');
await inputs.nth(0).setInputFiles({ name: "episode-01-es.mp4", mimeType: "video/mp4", buffer: Buffer.alloc(22 * mb) });
await page.locator('input[type="file"]').nth(0).setInputFiles([
  { name: "episode-01.mp4", mimeType: "video/mp4", buffer: Buffer.alloc(12 * mb) },
  {
    name: "key-frame.jpg",
    mimeType: "image/jpeg",
    buffer: await readFile(new URL("../public/mock/reference-frame.jpg", import.meta.url)),
  },
]);
// shadcn Select (Radix): open the combobox, then pick the option.
await page.getByRole("combobox", { name: "Source language" }).click();
await page.getByRole("option", { name: "Hindi (hi)" }).click();
await shot("new-review-files");
await page.getByRole("button", { name: "Upload and start review" }).click();
await page.waitForTimeout(700);
await shot("new-review-uploading");
await page.waitForURL(/\/reviews\/run_/, { timeout: 30000 });
await page.waitForTimeout(600);
await shot("review-running");

// Review detail (fixture with LLM-marked summary)
await page.goto(`${base}/reviews/rerun-es-v1-llm`);
await page.getByRole("heading", { name: "Summary" }).or(page.getByText("Summary").first()).waitFor();
await page.waitForFunction(() => [...document.querySelectorAll("video")].every((v) => v.readyState >= 1), null, {
  timeout: 20000,
});
await page.keyboard.press("j");
await page.keyboard.press("j");
await page.keyboard.press("o");
await page.waitForTimeout(800);
await page.evaluate(() => window.scrollTo(0, 0));
await shot("review-detail-1440");
// The page is ~4400 px tall: show the thread list further down with a viewport shot, not a full-page one.
await page.evaluate(() => window.scrollTo(0, 700));
await page.waitForTimeout(300);
await shot("review-detail-1440-threads");
await page.evaluate(() => window.scrollTo(0, 0));

// Filter: LLM findings only
await page.getByRole("button", { name: /LLM finding/ }).first().click();
await page.waitForTimeout(300);
await shot("review-detail-llm-filter");
await page.getByRole("button", { name: "Clear" }).click();

await page.setViewportSize({ width: 1024, height: 768 });
await page.waitForTimeout(400);
await noHorizontalOverflow("review detail 1024");
await shot("review-detail-1024");
await page.evaluate(() => window.scrollTo(0, 800));
await page.waitForTimeout(300);
await shot("review-detail-1024-threads");
await page.evaluate(() => window.scrollTo(0, 0));

// Sync check: seek via the transport and compare both players' currentTime.
await page.setViewportSize({ width: 1440, height: 900 });
await page.getByRole("button", { name: "Play both videos" }).click();
await page.waitForTimeout(2500);
const times = await page.evaluate(() => [...document.querySelectorAll("video")].map((v) => [v.paused, v.currentTime.toFixed(2)]));
console.log("players [paused, t] after play:", JSON.stringify(times));
await page.getByRole("button", { name: "Pause both videos" }).click();

// Feedback survives reload: accept the expanded thread, reload, look for the mark.
const sel = page.locator("li.ring-1").first();
await sel.getByRole("button", { name: /Accept/ }).last().click();
await sel.getByText("Accepted").first().waitFor();
await page.waitForTimeout(600);
await page.reload();
await page.getByText("Accepted").first().waitFor({ timeout: 10000 });
console.log("feedback after reload: 'Accepted' mark present");

// Cancel calls abort, and late progress events do not overwrite the reset state.
const aborts = [];
page.on("console", (m) => m.text().startsWith("[mock] abortUpload") && aborts.push(m.text()));
await page.goto(`${base}/reviews/new`);
await page.locator('input[type="file"]').nth(0).setInputFiles({ name: "big-es.mp4", mimeType: "video/mp4", buffer: Buffer.alloc(40 * mb) });
await page.getByRole("button", { name: "Upload and start review" }).click();
await page.getByText(/Uploading… [1-9]/).first().waitFor({ timeout: 10000 });
await page.getByRole("button", { name: "Cancel" }).click();
await page.getByRole("button", { name: "Upload and start review" }).waitFor();
await page.waitForTimeout(2000); // a late progress/"cancelled" event from the aborted upload would show up by now
const after = {
  progressbars: await page.getByRole("progressbar").count(),
  statusText: await page.getByText(/Uploading…|Cancelled|Upload failed/).count(),
  uploadButtonEnabled: await page.getByRole("button", { name: "Upload and start review" }).isEnabled(),
};
console.log("after cancel:", JSON.stringify(after), "abort calls:", aborts.length);
if (after.progressbars || after.statusText || !after.uploadButtonEnabled || aborts.length !== 1)
  overlayIssues.push(`cancel did not reset cleanly: ${JSON.stringify(after)}, abort calls ${aborts.length}`);
await shot("new-review-cancelled");
// Upload again after cancelling: starts from scratch and finishes (part 2 fails once and is retried).
await page.getByRole("button", { name: "Upload and start review" }).click();
await page.waitForURL(/\/reviews\/run_/, { timeout: 30000 });
console.log("re-upload after cancel: reached", page.url().replace(base, ""));

// Dark theme (Carbon g100): the app follows the OS setting until the user picks a theme.
await page.emulateMedia({ colorScheme: "dark" });
await page.setViewportSize({ width: 1440, height: 900 });
await page.goto(`${base}/`);
await page.getByRole("link", { name: "dub-01.mp4" }).first().waitFor();
await page.waitForTimeout(400);
await shot("dashboard-dark");
await page.goto(`${base}/reviews/rerun-es-v1-llm`);
await page.getByText("Summary").first().waitFor();
await page.keyboard.press("j");
await page.keyboard.press("o");
await page.waitForTimeout(800);
await page.evaluate(() => window.scrollTo(0, 0));
await shot("review-detail-dark");

if (errors.length) console.log("browser errors:\n" + errors.join("\n"));
await browser.close();
if (overlayIssues.length) {
  console.log("PROBLEMS:\n" + overlayIssues.join("\n"));
  process.exit(1);
}
console.log("OK: no dev-overlay issues, no horizontal overflow");
