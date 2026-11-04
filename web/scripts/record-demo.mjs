// Records the demo walkthrough of one finished review on the live site (Playwright video, 1440x900).
// Usage: E2E_EMAIL=.. E2E_PW=.. RUN=run_x DUR='{"s2":10.3,...}' OUT=/tmp/demo/rec node scripts/record-demo.mjs
// Prints segment start offsets (seconds from the start of the video) as JSON on the last line.
import { chromium } from "playwright-core";

const base = process.env.BASE ?? "http://localhost:53000";
const run = process.env.RUN;
const dur = JSON.parse(process.env.DUR);
const out = process.env.OUT ?? "/tmp/demo/rec";
const browser = await chromium.launch({ executablePath: "/usr/bin/google-chrome" });

// Log in outside the recording.
const login = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const lp = await login.newPage();
await lp.goto(base + "/login");
await lp.fill("input[type=email]", process.env.E2E_EMAIL);
await lp.fill("input[type=password]", process.env.E2E_PW);
await lp.click("button[type=submit]");
await lp.waitForURL((u) => !u.pathname.startsWith("/login"));
const state = await login.storageState();
await login.close();

const ctx = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  storageState: state,
  recordVideo: { dir: out, size: { width: 1440, height: 900 } },
});
// Visible cursor (headless recordings have none).
await ctx.addInitScript(() => {
  addEventListener("DOMContentLoaded", () => {
    const d = document.createElement("div");
    d.style.cssText =
      "position:fixed;z-index:99999;width:18px;height:18px;border-radius:50%;background:rgba(15,98,254,.35);" +
      "border:2px solid #0f62fe;pointer-events:none;left:-40px;top:-40px;transform:translate(-50%,-50%);transition:width .1s,height .1s";
    document.body.appendChild(d);
    addEventListener("mousemove", (e) => { d.style.left = e.clientX + "px"; d.style.top = e.clientY + "px"; });
    addEventListener("mousedown", () => { d.style.width = d.style.height = "28px"; });
    addEventListener("mouseup", () => { d.style.width = d.style.height = "18px"; });
  });
});
const page = await ctx.newPage();
const t0 = Date.now();
const now = () => (Date.now() - t0) / 1000;
const marks = {};
let segEnd = 0;
const seg = async (name, fn) => {
  marks[name] = now();
  segEnd = marks[name] + dur[name] + 0.9;
  await fn();
  const left = segEnd - now();
  if (left > 0) await page.waitForTimeout(left * 1000);
};
const moveTo = async (loc) => {
  await loc.scrollIntoViewIfNeeded();
  const b = await loc.boundingBox();
  await page.mouse.move(b.x + Math.min(b.width / 2, 120), b.y + b.height / 2, { steps: 25 });
  await page.waitForTimeout(250);
};
const click = async (loc) => { await moveTo(loc); await page.mouse.down(); await page.mouse.up(); };
const smooth = async (loc, block = "center") => {
  await loc.evaluate((el, block) => el.scrollIntoView({ behavior: "smooth", block }), block);
  await page.waitForTimeout(1200);
};

await page.goto(base + "/");
await page.getByText("dub-es-60s.mp4").first().waitFor();
await page.waitForTimeout(800);
const lead = now();

await seg("s2", async () => {
  await page.mouse.move(700, 250, { steps: 20 });
  await moveTo(page.getByText("Changes requested").nth(1));
  await page.waitForTimeout(2500);
  await moveTo(page.getByText("dub-es-60s.mp4").first());
});

await seg("s3", async () => {
  await click(page.getByText("dub-es-60s.mp4").first());
  await page.getByRole("button", { name: /^Pipeline/ }).waitFor();
  await page.waitForTimeout(1500);
  await moveTo(page.getByText(/LLM-written · meta:muse-spark-1.3/).first());
});

await seg("s4", async () => {
  await click(page.getByRole("button", { name: /^Pipeline/ }));
  await page.waitForTimeout(2500);
  await smooth(page.getByText("Evaluate dubbed tracks").first());
  await moveTo(page.getByText("gateway:gemini-flash").first());
  await page.waitForTimeout(2500);
  await smooth(page.getByText("Audio-video judge").first());
  await moveTo(page.getByText(/disputed\s*8/).first());
});

await seg("s5", async () => {
  await click(page.getByRole("button", { name: /^Pipeline/ }));
  await page.evaluate(() => scrollTo({ top: 0, behavior: "smooth" }));
  await page.waitForTimeout(900);
  await click(page.getByText("Dub ends 2 seconds early on visible speaker"));
  await page.waitForTimeout(600);
  await click(page.getByRole("button", { name: "Play" }).first());
});

await seg("s6", async () => {
  await click(page.getByRole("button", { name: /Pause/ }).first()).catch(() => {});
  await click(page.getByText("Likely missing dub leaves silent speaking"));
  await page.waitForTimeout(800);
  const reason = page.getByText(/Disputed by meta:muse-spark-1.3/).first();
  await smooth(reason);
  await moveTo(reason);
});

await seg("s7", async () => {
  await click(page.getByText("Lip-sync mismatch with early start"));
  await page.waitForTimeout(800);
  const card = page.locator("li", { hasText: "av_judge:hi->es:A001" }).last();
  await smooth(card);
  await moveTo(card.getByText("LIP_SYNC"));
  await page.waitForTimeout(3500);
  await moveTo(card.getByRole("button", { name: "False positive" }));
});

const total = now();
await ctx.close();
await browser.close();
console.log(JSON.stringify({ lead, total, marks }));
