"use strict";
/* End-to-end smoke test of the built page in a real browser.
 *
 *   python -m casecal demo --out dist-demo
 *   node tests/e2e/smoke.js dist-demo [screenshot-dir]
 *
 * Needs Playwright with Chromium; it is not part of CI. It fails on any console
 * error, which is how a Content-Security-Policy violation would show up.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium, devices } = require("playwright");

const siteDir = path.resolve(process.argv[2] || "dist-demo");
const shotDir = process.argv[3] ? path.resolve(process.argv[3]) : null;
const FAMILY = process.env.E2E_FAMILY || "demo-famiglia-calendario";
const OWNER = process.env.E2E_OWNER || "demo-proprieta-calendario";
const SCALE = Number(process.env.E2E_SCALE || 1);

function serve() {
  const server = http.createServer((request, response) => {
    const name = request.url === "/" ? "index.html" : path.basename(request.url.split("?")[0]);
    fs.readFile(path.join(siteDir, name), (error, body) => {
      if (error) { response.writeHead(404); response.end(); return; }
      response.writeHead(200, { "Content-Type": name.endsWith(".html") ? "text/html; charset=utf-8" : "text/plain" });
      response.end(body);
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

async function main() {
  const server = await serve();
  const url = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch();
  const problems = [];

  async function open(options) {
    const context = await browser.newContext(options);
    const page = await context.newPage();
    page.on("console", (message) => { if (message.type() === "error") problems.push(message.text()); });
    page.on("pageerror", (error) => problems.push(String(error)));
    page.on("request", (request) => {
      if (!request.url().startsWith(url) && !request.url().startsWith("data:")) problems.push("external request: " + request.url());
    });
    await page.goto(url);
    return { context, page };
  }
  async function shot(page, name, fullPage = true) {
    if (!shotDir) return;
    fs.mkdirSync(shotDir, { recursive: true });
    await page.screenshot({ path: path.join(shotDir, name + ".png"), fullPage });
  }
  /**
   * The calendar's promise: every bar starts at the check-in hour of its first day and
   * ends at the check-out hour of its last one. Checked against the real layout.
   */
  async function barsFollowTheHouseHours(page) {
    const report = await page.evaluate(() => {
      const IN = 15 / 24, OUT = 11 / 24;
      const result = { starts: 0, ends: 0, worst: 0, clipped: [], turnovers: 0 };
      const rowOf = (bar) => bar.closest(".week");
      for (const bar of document.querySelectorAll(".months .bar")) {
        const box = bar.getBoundingClientRect();
        const tile = (day) => rowOf(bar).querySelector(`.day[data-day="${day}"]`);
        if (!bar.classList.contains("cut-l")) {
          const first = tile(bar.dataset.from).getBoundingClientRect();
          result.worst = Math.max(result.worst, Math.abs(box.left - (first.left + IN * first.width)));
          result.starts++;
        }
        if (!bar.classList.contains("cut-r")) {
          const last = tile(bar.dataset.to).getBoundingClientRect();
          result.worst = Math.max(result.worst, Math.abs(box.right - (last.left + OUT * last.width)));
          result.ends++;
        }
        const text = bar.querySelector(".txt");
        if (text.textContent && text.getBoundingClientRect().right > box.right + 0.5) result.clipped.push(text.textContent);
        const tag = bar.querySelector(".tag");
        if (!tag.hidden && tag.getBoundingClientRect().right > box.right + 0.5) result.clipped.push("tag");
      }
      // Same-day turnovers: the bar that leaves must stop before the one that arrives begins.
      for (const lane of document.querySelectorAll(".months .lane")) {
        const bars = [...lane.querySelectorAll(".bar")];
        for (let i = 1; i < bars.length; i++) {
          if (bars[i - 1].dataset.to !== bars[i].dataset.from) continue;
          const gap = bars[i].getBoundingClientRect().left - bars[i - 1].getBoundingClientRect().right;
          const width = lane.closest(".week").querySelector(".day").getBoundingClientRect().width;
          result.worst = Math.max(result.worst, Math.abs(gap - (IN - OUT) * width));
          result.turnovers++;
        }
      }
      return result;
    });
    assert.ok(report.starts > 5 && report.ends > 5, "bars with real starts and ends were measured");
    assert.ok(report.turnovers > 0, "the demo contains a same-day turnover");
    assert.ok(report.worst < 1, `a bar is ${report.worst.toFixed(2)}px away from its hour`);
    assert.deepEqual(report.clipped, [], "labels that do not fit must not be drawn");
  }

  async function noSidewaysScroll(page) {
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 0, `page scrolls sideways by ${overflow}px`);
  }

  /* ---- desktop, light, family passphrase ---- */
  let { context, page } = await open({ viewport: { width: 1280, height: 900 }, colorScheme: "light", deviceScaleFactor: SCALE });
  await page.waitForSelector(".lock-card");
  assert.equal(await page.locator(".shell").count(), 0, "nothing is shown before unlocking");
  await shot(page, "01-lock", false);

  await page.fill("#pw", "non-e-la-password-giusta");
  await page.click("button[type=submit]");
  await page.waitForSelector(".error:not([hidden])");
  assert.match(await page.textContent(".error"), /non corretta/);

  await page.fill("#pw", FAMILY);
  await page.click("button[type=submit]");
  await page.waitForSelector(".shell");
  assert.equal(await page.locator(".house").count(), 2);
  assert.ok((await page.locator(".month").count()) >= 2, "months rendered");
  assert.ok((await page.locator(".months .bar").count()) > 5, "stays drawn as bars");
  assert.match(await page.textContent(".hours"), /Check-in dalle 15:00, check-out entro le 11:00/);
  assert.deepEqual(await page.locator(".keyart .cap").allTextContents(), ["15:00", "11:00"]);
  await barsFollowTheHouseHours(page);
  const labels = await page.locator(".months .bar .txt").allTextContents();
  assert.ok(labels.some((text) => /ospiti/.test(text)), "guest counts written inside bars");
  assert.ok(labels.includes("Bloccata"), "blocked periods labelled");
  assert.match(await page.textContent(".stays thead"), /Check-indalle 15:00Check-outentro le 11:00/);
  assert.equal(await page.locator(".day.today").count(), 1);
  assert.equal(await page.locator(".owner-lock").count(), 1, "economics stay locked for the family");
  assert.equal(await page.locator(".tiles").count(), 0);
  assert.ok(!(await page.textContent("#app")).includes("Netto proprietario"), "no economics rendered for the family");
  await noSidewaysScroll(page);
  await shot(page, "02-family-desktop");

  await page.hover(".day.today");
  await page.waitForSelector(".tip:not([hidden])");
  assert.equal(await page.locator(".tip .tip-row").count(), 2, "one readout row per house");
  assert.match(await page.textContent(".tip"), /Ospiti · Casa Maredalle 15:00 di .+ alle 11:00 di /);
  assert.match(await page.textContent(".tip"), /Libera · Casa Lagodalle 11:00 \(check-out al mattino\)/);
  await shot(page, "03-tooltip", false);

  await page.focus(".day.today");
  const before = await page.evaluate(() => document.activeElement.getAttribute("data-day"));
  await page.keyboard.press("ArrowRight");
  const after = await page.evaluate(() => document.activeElement.getAttribute("data-day"));
  assert.equal(Number(after), Number(before) + 1, "arrow keys walk the days");
  assert.match(await page.evaluate(() => document.activeElement.getAttribute("aria-label")), /Casa Mare: .+ Casa Lago: /);

  const barsBefore = await page.locator(".months .bar").count();
  await page.locator(".seg input[name=filter]").nth(2).check({ force: true });
  await page.waitForFunction(() => document.querySelectorAll(".house").length === 1);
  assert.equal(await page.locator(".months.single").count(), 1);
  assert.ok((await page.locator(".months .bar").count()) < barsBefore, "the filter removes the other house's bars");
  await barsFollowTheHouseHours(page);
  assert.equal(await page.locator(".lane.p1").count() > 0, true, "the remaining house keeps its own hue");
  assert.equal(await page.locator(".lane.p0").count(), 0);
  await shot(page, "04-filter-one-house");
  await page.locator(".seg input[name=filter]").nth(0).check({ force: true });
  await page.waitForFunction(() => document.querySelectorAll(".house").length === 2);

  await page.click("text=Mostra i mesi passati");
  assert.ok((await page.locator(".month.past").count()) >= 1 || new Date().getMonth() === 0);

  await page.reload();
  await page.waitForSelector(".shell");
  assert.equal(await page.locator(".lock-card").count(), 0, "the device remembers the family key");

  await page.fill("#owner-pw", FAMILY);
  await page.click(".owner-lock button[type=submit]");
  await page.waitForSelector(".owner-lock .error:not([hidden])");
  assert.equal(await page.locator(".tiles").count(), 0, "the family passphrase does not open the economics");

  await page.fill("#owner-pw", OWNER);
  await page.click(".owner-lock button[type=submit]");
  await page.waitForSelector(".tiles");
  assert.equal(await page.locator(".tile").count(), 6);
  assert.equal(await page.locator(".kpi-tables h4", { hasText: "Statistiche mensili" }).count(), 2);
  assert.equal(await page.locator(".kpi-tables h4", { hasText: "Prenotazioni con check-in" }).count(), 2);
  const headers = await page.locator(".kpi-tables table").first().locator("thead th").allTextContents();
  assert.deepEqual(headers, ["Mese", "Notti", "Occ.", "ADR", "Lordo*", "Imposta", "Netto proprietario", "Netto/lordo*"]);
  assert.ok((await page.locator(".col").count()) > 0, "monthly chart drawn");
  await page.locator(".group").nth(7).hover();
  await page.waitForSelector(".tip:not([hidden])");
  await shot(page, "05-owner-desktop");
  await noSidewaysScroll(page);

  await page.click("text=Chiudi area");
  await page.waitForSelector(".owner-lock");
  await page.click("text=Blocca");
  await page.waitForSelector(".lock-card");
  await page.reload();
  await page.waitForSelector(".lock-card");
  assert.equal(await page.locator(".shell").count(), 0, "locking forgets the key");
  await context.close();

  /* ---- phone, dark, owner passphrase opens everything ---- */
  ({ context, page } = await open({ ...devices["iPhone 13"], colorScheme: "dark" }));
  await page.waitForSelector(".lock-card");
  await page.fill("#pw", OWNER);
  await page.click("button[type=submit]");
  await page.waitForSelector(".tiles");
  assert.equal(await page.locator(".house").count(), 2);
  await noSidewaysScroll(page);
  await barsFollowTheHouseHours(page);
  await shot(page, "06-owner-phone-dark");
  // Bars are narrower in landscape or after a resize: labels must be fitted again.
  await page.setViewportSize({ width: 844, height: 390 });
  await page.waitForTimeout(150);
  await barsFollowTheHouseHours(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(150);
  await barsFollowTheHouseHours(page);
  await page.locator(".day.today").tap();
  await page.waitForSelector(".tip:not([hidden])");
  await shot(page, "07-phone-tooltip", false);
  await page.locator(".day.today").tap();
  await page.waitForSelector(".tip", { state: "hidden" });
  await page.locator(".day.today").tap();
  await page.waitForSelector(".tip:not([hidden])");
  await page.locator(".top h1").tap();
  await page.waitForSelector(".tip", { state: "hidden" });
  await context.close();

  await browser.close();
  server.close();
  assert.deepEqual(problems, [], "console errors or external requests");
  console.log("smoke test passed");
}

main().catch((error) => { console.error(error); process.exit(1); });
