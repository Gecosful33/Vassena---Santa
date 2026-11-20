"use strict";
/* End-to-end smoke test of the built page in a real browser.
 *
 *   python -m casecal demo --out dist-demo
 *   node tests/e2e/smoke.js dist-demo [screenshot-dir]
 *
 * Needs Playwright with Chromium; it is not part of CI. It fails on any console
 * error, which is how a Content-Security-Policy violation would show up.
 *
 * The family's entries are tried twice: on the demo page, which keeps them in the
 * browser, and on a page wired to the GitHub API. GitHub is played here by a stand-in
 * shared by two "devices", and what the page wrote to it is finally read back by the
 * Python code of the nightly job.
 */
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { chromium, devices } = require("playwright");

const ROOT = path.resolve(__dirname, "..", "..");
const siteDir = path.resolve(process.argv[2] || "dist-demo");
const shotDir = process.argv[3] ? path.resolve(process.argv[3]) : null;
const FAMILY = process.env.E2E_FAMILY || "demo-famiglia-calendario";
const OWNER = process.env.E2E_OWNER || "demo-proprieta-calendario";
const SCALE = Number(process.env.E2E_SCALE || 1);
const API = "https://api.github.com";
const LOG_PATH = "/repos/owner/repo/issues/7/comments";
const TOKEN = "github_pat_E2E";

/* The demo page again, but with the settings the nightly job writes when the family log is on. */
const BUILD_LOG_PAGE = `
import sys
from datetime import datetime, timezone
from casecal.demo import DEMO_FAMILY_PASSPHRASE, DEMO_OWNER_PASSPHRASE, demo_snapshot
from casecal.sync import publish
snapshot = demo_snapshot(datetime.now(timezone.utc).replace(microsecond=0))
snapshot.log = {"kind": "github", "api": "${API}", "repo": "owner/repo", "issue": 7,
                "token": "${TOKEN}", "cursor": 1000, "since": "2026-01-01T00:00:00Z"}
publish(snapshot, family_passphrase=DEMO_FAMILY_PASSPHRASE, owner_passphrase=DEMO_OWNER_PASSPHRASE,
        site_id="casecal-demo", out_dir=sys.argv[1])
`;
/* What the nightly job would make of the comments the page wrote. */
const READ_LOG_BACK = `
import json, sys
from casecal.crypto import derive_key, site_salt
from casecal.demo import DEMO_FAMILY_PASSPHRASE
from casecal.family import decode_event, fold
key = derive_key(DEMO_FAMILY_PASSPHRASE, site_salt("casecal-demo"))
events = [decode_event(key, comment["body"]) for comment in json.load(open(sys.argv[1], encoding="utf-8"))]
assert all(events), "a comment written by the page did not open with the family key"
entries = sorted(fold(events).values(), key=lambda entry: entry.who)
print(json.dumps({"kinds": [event["t"] for event in events],
                  "entries": [[entry.who, entry.by, entry.note] for entry in entries]}))
`;

function serve(directory) {
  const server = http.createServer((request, response) => {
    const name = request.url === "/" ? "index.html" : path.basename(request.url.split("?")[0]);
    fs.readFile(path.join(directory, name), (error, body) => {
      if (error) { response.writeHead(404); response.end(); return; }
      response.writeHead(200, { "Content-Type": name.endsWith(".html") ? "text/html; charset=utf-8" : "text/plain" });
      response.end(body);
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

/** GitHub's issue-comments endpoint, with the same paging, status codes and CORS answers. */
function fakeGitHub() {
  const github = { comments: [], nextId: 1001, failWith: null, calls: [] };
  const cors = {
    "access-control-allow-origin": "*",
    "access-control-expose-headers": "ETag, Link, x-ratelimit-limit, x-ratelimit-remaining, x-ratelimit-reset",
    "content-type": "application/json; charset=utf-8"
  };
  github.handle = async (route) => {
    const request = route.request();
    const target = new URL(request.url());
    github.calls.push({ method: request.method(), path: target.pathname, query: target.search, headers: request.headers() });
    if (github.failWith === "offline") return route.abort("internetdisconnected");
    if (github.failWith) return route.fulfill({ status: github.failWith, headers: cors, body: JSON.stringify({ message: "nope" }) });
    if (target.pathname !== LOG_PATH) return route.fulfill({ status: 404, headers: cors, body: "{}" });
    if (request.method() === "POST") {
      const comment = { id: github.nextId++, body: JSON.parse(request.postData()).body,
        created_at: new Date().toISOString().replace(/\.\d+Z$/, "Z") };
      github.comments.push(comment);
      return route.fulfill({ status: 201, headers: cors, body: JSON.stringify(comment) });
    }
    const size = Number(target.searchParams.get("per_page")), page = Number(target.searchParams.get("page"));
    return route.fulfill({ status: 200, headers: cors, body: JSON.stringify(github.comments.slice((page - 1) * size, page * size)) });
  };
  return github;
}

async function main() {
  const server = await serve(siteDir);
  const url = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch();
  const problems = [];
  const touchPages = new WeakSet();
  let apiFailuresExpected = false;

  /** Open the page at `target`; `github`, when given, plays the API for this "device". */
  async function open(options, target = url, github = null) {
    const context = await browser.newContext(options);
    if (github) await context.route(API + "/**", github.handle);
    const page = await context.newPage();
    page.on("console", (message) => {
      if (message.type() !== "error") return;
      // A refused or failed API call is logged by the browser itself; here it is the scenario.
      if (apiFailuresExpected && message.location().url.startsWith(API)) return;
      problems.push(message.text());
    });
    page.on("pageerror", (error) => problems.push(String(error)));
    page.on("request", (request) => {
      const address = request.url();
      if (address.startsWith(target) || address.startsWith("data:")) return;
      if (github && address.startsWith(API + LOG_PATH)) return;
      problems.push("external request: " + address);
    });
    if (options.hasTouch) touchPages.add(page);
    await page.goto(target);
    return { context, page };
  }
  async function shot(page, name, fullPage = true) {
    if (!shotDir) return;
    fs.mkdirSync(shotDir, { recursive: true });
    await page.screenshot({ path: path.join(shotDir, name + ".png"), fullPage });
  }
  /**
   * Press a control the way this device does: a finger on a phone, the mouse elsewhere.
   * A phone has no mouse at all, and a stray mouse position would make the browser
   * report a hover over whatever a closing sheet uncovers.
   */
  const press = (page, target) => {
    const locator = typeof target === "string" ? page.locator(target) : target;
    return touchPages.has(page) ? locator.tap() : locator.click();
  };
  async function unlock(page, passphrase) {
    await page.waitForSelector(".lock-card");
    await page.fill("#pw", passphrase);
    await press(page, "button[type=submit]");
    await page.waitForSelector(".shell");
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
        // A family period is drawn over the manager's bars: nothing may be written beneath it.
        if (!bar.classList.contains("family")) {
          for (const cover of bar.parentElement.querySelectorAll(".bar.family")) {
            const over = cover.getBoundingClientRect();
            if (over.right <= box.left || over.left >= box.right) continue;
            const hidden = (node) => node.getBoundingClientRect().right > over.left + 0.5 && node.getBoundingClientRect().left < over.right;
            if (text.textContent && hidden(text)) result.clipped.push("under a family period: " + text.textContent);
            if (!tag.hidden && hidden(tag)) result.clipped.push("tag under a family period");
          }
        }
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

  /**
   * Every table is laid out as a table: header, rows and totals one under the other,
   * each cell under its heading. (A class name shared with the calendar once turned
   * the monthly statistics into a grid, with the three parts side by side.)
   */
  async function tablesKeepTheirShape(page, atLeast) {
    const report = await page.evaluate(() => {
      const expected = { THEAD: "table-header-group", TBODY: "table-row-group", TFOOT: "table-footer-group" };
      const display = (node) => getComputedStyle(node).display;
      const tables = [...document.querySelectorAll("table")];
      const problems = [];
      for (const table of tables) {
        const say = (what) => problems.push(`${table.className}: ${what}`);
        if (display(table) !== "table") say("laid out as " + display(table));
        const groups = [...table.children], boxes = groups.map((group) => group.getBoundingClientRect());
        groups.forEach((group, i) => {
          if (display(group) !== expected[group.tagName]) say(group.tagName + " laid out as " + display(group));
          if (i && boxes[i].top < boxes[i - 1].bottom - 0.5) say(group.tagName + " is not below " + groups[i - 1].tagName);
          if (i && Math.abs(boxes[i].left - boxes[0].left) > 0.5) say(group.tagName + " does not start where the header does");
        });
        const headings = [...table.querySelectorAll("thead th")];
        for (const row of table.querySelectorAll("tbody tr, tfoot tr")) {
          const cells = [...row.children];
          if (cells.length !== headings.length) continue; // a row that spans columns
          cells.forEach((cell, i) => {
            const own = cell.getBoundingClientRect(), above = headings[i].getBoundingClientRect();
            if (Math.abs(own.left - above.left) > 0.5 || Math.abs(own.right - above.right) > 0.5) {
              say(`"${cell.textContent}" is not under "${headings[i].textContent}"`);
            }
          });
        }
      }
      return { count: tables.length, problems: problems.slice(0, 8) };
    });
    assert.ok(report.count >= atLeast, `expected at least ${atLeast} tables, found ${report.count}`);
    assert.deepEqual(report.problems, [], "a table lost its shape");
  }

  async function noSidewaysScroll(page) {
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 0, `page scrolls sideways by ${overflow}px`);
  }

  /** A sheet holds everything it shows: no field sticks out, nothing scrolls sideways. */
  async function sheetFits(page) {
    const report = await page.evaluate(() => {
      const dialog = document.querySelector("dialog.sheet");
      const box = dialog.getBoundingClientRect();
      const out = [...dialog.querySelectorAll(".input, .btn, p")].filter((node) => {
        const own = node.getBoundingClientRect();
        return own.width > 0 && (own.left < box.left - 0.5 || own.right > box.right + 0.5);
      }).map((node) => node.id || node.className || node.tagName);
      return { sideways: dialog.scrollWidth - dialog.clientWidth, out, inView: box.left >= 0 && box.right <= window.innerWidth };
    });
    assert.deepEqual(report, { sideways: 0, out: [], inView: true }, "the sheet does not hold its content");
  }

  /* ---- helpers for the family's entries ---- */
  const names = (page) => page.locator(".people .entry-who strong").allTextContents();
  const sorted = (list) => [...list].sort();
  async function waitForNames(page, expected) {
    await page.waitForFunction((wanted) => {
      const shown = [...document.querySelectorAll(".people .entry-who strong")].map((node) => node.textContent).sort();
      return JSON.stringify(shown) === JSON.stringify(wanted);
    }, sorted(expected));
  }
  const sheetGone = (page) => page.waitForSelector("dialog.sheet", { state: "detached" });
  const closeSheet = async (page) => {
    await press(page, "dialog.sheet .sheet-head button");
    await sheetGone(page);
  };
  async function openAddForm(page) {
    await press(page, '[data-key="family-add"]');
    await page.waitForSelector("dialog.sheet[open] form");
  }
  async function openEditForm(page, who) {
    await press(page, page.locator(".people .entry", { hasText: who }).locator("button"));
    await page.waitForSelector("dialog.sheet[open] form");
    assert.equal(await page.inputValue("#entry-who"), who);
  }
  const submit = (page) => press(page, "dialog.sheet button[type=submit]");
  /** Add a period on the dates the form proposes; returns them. */
  async function addPeriod(page, who, me, note = "") {
    await openAddForm(page);
    assert.equal(await page.locator(".preview .ico-critical").count(), 0, "the proposed dates can be saved as they are");
    const dates = { from: await page.inputValue("#entry-from"), to: await page.inputValue("#entry-to") };
    await page.fill("#entry-who", who);
    if (note) await page.fill("#entry-note", note);
    await page.fill("#entry-me", me);
    await submit(page);
    await sheetGone(page);
    return dates;
  }
  /** Make the page read the log again, as it does when its tab comes back into view. */
  const comeBack = (page) => page.evaluate(() => { document.dispatchEvent(new Event("visibilitychange")); });
  const syncText = (page) => page.locator(".people").innerText();

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
  await tablesKeepTheirShape(page, 2); // the stays of each house

  // The family's own periods: listed, drawn over the calendar, each with its standing on the portal.
  const demoNames = await names(page);
  assert.deepEqual(sorted(demoNames), ["Elvio e famiglia", "Nonna Ada"]);
  const standing = await page.locator(".people .entry-state strong").allTextContents();
  assert.deepEqual(sorted(standing), ["Bloccata sul portale", "Da bloccare sul portale"]);
  assert.match(await page.locator(".people .entry", { hasText: "Nonna Ada" }).innerText(), /ancora in vendita: /);
  assert.ok((await page.locator(".months .bar.family.blocked").count()) >= 1, "a protected family period is drawn");
  assert.ok((await page.locator(".months .bar.family.pending").count()) >= 1, "an unprotected one is drawn differently");
  const familyLabels = await page.locator(".months .bar.family .txt").allTextContents();
  assert.ok(familyLabels.some((text) => text.startsWith("Elvio e famiglia")), "family bars carry the name");
  assert.match(await syncText(page), /Versione dimostrativa: le modifiche restano su questo dispositivo/);
  await shot(page, "02-family-desktop");

  await page.hover(".day.today");
  await page.waitForSelector(".tip:not([hidden])");
  assert.equal(await page.locator(".tip .tip-row").count(), 2, "one readout row per house");
  const marks = await page.locator(".tip .tip-row .dash").evaluateAll((nodes) => nodes.map((node) => {
    const box = node.getBoundingClientRect();
    return [Math.round(box.width), Math.round(box.height), getComputedStyle(node).backgroundColor];
  }));
  assert.equal(marks.length, 2);
  for (const [width, height] of marks) assert.deepEqual([width, height], [14, 3], "each row carries its house's mark");
  assert.notEqual(marks[0][2], marks[1][2], "in that house's colour");
  assert.match(await page.textContent(".tip"), /Ospiti · Casa Maredalle 15:00 di .+ alle 11:00 di /);
  assert.match(await page.textContent(".tip"), /Libera · Casa Lagodalle 11:00 \(check-out al mattino\)/);
  await shot(page, "03-tooltip", false);
  const familyDay = await page.locator(".months .bar.family.pending").first().getAttribute("data-from");
  await page.hover(`.day[data-day="${familyDay}"]`);
  await page.waitForFunction(() => /Uso famiglia: Nonna Ada \(da bloccare sul portale\)/.test(document.querySelector(".tip").textContent));

  // Keyboard: arrows walk the days, Enter opens the day's sheet, Escape gives the focus back.
  await page.focus(".day.today");
  const before = await page.evaluate(() => document.activeElement.getAttribute("data-day"));
  await page.keyboard.press("ArrowRight");
  const after = await page.evaluate(() => document.activeElement.getAttribute("data-day"));
  assert.equal(Number(after), Number(before) + 1, "arrow keys walk the days");
  assert.match(await page.evaluate(() => document.activeElement.getAttribute("aria-label")), /Casa Mare: .+ Casa Lago: /);
  await page.keyboard.press("Enter");
  await page.waitForSelector("dialog.sheet[open]");
  assert.equal(await page.locator("dialog.sheet .sheet-house").count(), 2, "the day's sheet has one block per house");
  assert.equal(await page.locator(".tip:not([hidden])").count(), 0, "the readout makes way for the sheet");
  await sheetFits(page);
  await shot(page, "04-day-sheet", false);
  await page.keyboard.press("Escape");
  await sheetGone(page);
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute("data-day")), after, "focus returns to the day");

  const barsBefore = await page.locator(".months .bar").count();
  await page.locator(".seg input[name=filter]").nth(2).check({ force: true });
  await page.waitForFunction(() => document.querySelectorAll(".house").length === 1);
  assert.equal(await page.locator(".months.single").count(), 1);
  assert.ok((await page.locator(".months .bar").count()) < barsBefore, "the filter removes the other house's bars");
  await barsFollowTheHouseHours(page);
  assert.equal(await page.locator(".lane.p1").count() > 0, true, "the remaining house keeps its own hue");
  assert.equal(await page.locator(".lane.p0").count(), 0);
  assert.deepEqual(await names(page), ["Nonna Ada"], "and only its family periods stay listed");
  await shot(page, "05-filter-one-house");
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
  assert.equal(await page.locator(".todo").count(), 0);

  await page.fill("#owner-pw", OWNER);
  await page.click(".owner-lock button[type=submit]");
  await page.waitForSelector(".tiles");
  assert.equal(await page.locator(".tile").count(), 6);
  assert.equal(await page.locator(".kpi-tables h4", { hasText: "Statistiche mensili" }).count(), 2);
  assert.equal(await page.locator(".kpi-tables h4", { hasText: "Prenotazioni con check-in" }).count(), 2);
  const headers = await page.locator(".kpi-tables table").first().locator("thead th").allTextContents();
  assert.deepEqual(headers, ["Mese", "Notti", "Occ.", "ADR", "Lordo*", "Imposta", "Netto proprietario", "Netto/lordo*"]);
  const monthNames = await page.locator(".kpi-tables table").first().locator("tbody td:first-child").allTextContents();
  assert.deepEqual([monthNames.length, monthNames[0], monthNames[11]], [12, "Gennaio", "Dicembre"]);
  assert.match(await page.locator(".kpi-tables table").first().locator("tfoot").innerText(), /^Totale \d{4}\s/);
  await tablesKeepTheirShape(page, 6); // stays, monthly statistics and reservations of each house
  assert.ok((await page.locator(".col").count()) > 0, "monthly chart drawn");
  // The owner's to-do: the family periods whose nights are still for sale, and only those.
  const todo = await page.locator(".todo").innerText();
  assert.match(todo, /Da chiudere sul portale del gestore/);
  assert.match(todo, /Casa Lago\s+.+ → .+ · Nonna Ada/);
  assert.ok(!todo.includes("Elvio e famiglia"), "a period already blocked needs no action");
  await page.locator(".group").nth(7).hover();
  await page.waitForSelector(".tip:not([hidden])");
  await shot(page, "06-owner-desktop");
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
  await unlock(page, OWNER);
  await page.waitForSelector(".tiles");
  assert.equal(await page.locator(".house").count(), 2);
  await noSidewaysScroll(page);
  await tablesKeepTheirShape(page, 6);
  await barsFollowTheHouseHours(page);
  await shot(page, "07-owner-phone-dark");
  // Bars are narrower in landscape or after a resize: labels must be fitted again.
  await page.setViewportSize({ width: 844, height: 390 });
  await page.waitForTimeout(150);
  await barsFollowTheHouseHours(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(150);
  await barsFollowTheHouseHours(page);
  // A tap on a day opens its sheet; no hover readout is left behind when it closes.
  await page.locator(".day.today").tap();
  await page.waitForSelector("dialog.sheet[open]");
  assert.match(await page.locator("dialog.sheet").innerText(), /Casa Mare\s+Ospiti · dalle 15:00/);
  assert.match(await page.locator("dialog.sheet").innerText(), /Casa Lago\s+Libera · /);
  await sheetFits(page);
  await shot(page, "08-phone-day-sheet", false);
  await closeSheet(page);
  assert.equal(await page.locator(".tip:not([hidden])").count(), 0);
  await context.close();

  /* ---- phone, light: the family adds, changes and removes its periods (demo: kept in the browser) ---- */
  ({ context, page } = await open({ ...devices["iPhone 13"], colorScheme: "light" }));
  await unlock(page, FAMILY);
  // From a free day of the calendar.
  const lakeDay = await page.locator(".people .entry", { hasText: "Nonna Ada" }).evaluate(() => {
    // The day before Nonna Ada's pending period at the lake house is free there, by construction of the demo.
    return Number(document.querySelector(".months .bar.family.pending").dataset.from) - 1;
  });
  await page.locator(`.day[data-day="${lakeDay}"]`).tap();
  await page.waitForSelector("dialog.sheet[open]");
  await page.locator("dialog.sheet .sheet-house", { hasText: "Casa Lago" }).locator("button", { hasText: "Segna un periodo da questo giorno" }).tap();
  await page.waitForSelector("dialog.sheet[open] form");
  assert.equal(await page.inputValue("#entry-house"), "lago");
  assert.equal(await page.evaluate(() => Math.round(Date.parse(document.querySelector("#entry-from").value) / 86400000)), lakeDay);
  // One night only: the next one belongs to Nonna Ada.
  assert.equal(await page.evaluate(() => Math.round(Date.parse(document.querySelector("#entry-to").value) / 86400000)), lakeDay + 1);
  await closeSheet(page);

  // From the list: nothing is saved without a name; then it is, and it survives a reload.
  await openAddForm(page);
  assert.match(await page.locator(".preview").innerText(), /^Dalle 15:00 di .+ alle 11:00 di .+, \d+ nott[ei]\. /);
  await submit(page);
  await page.waitForSelector("dialog.sheet .error:not([hidden])");
  assert.match(await page.locator("dialog.sheet .error").innerText(), /Scrivi chi usa la casa/);
  await page.fill("#entry-who", "Zia Carla");
  await page.fill("#entry-me", "");
  await submit(page);
  assert.match(await page.locator("dialog.sheet .error").innerText(), /Scrivi il tuo nome/);
  await page.fill("#entry-me", "Luca");
  await page.fill("#entry-note", "arriva in treno");
  const carla = { from: await page.inputValue("#entry-from"), to: await page.inputValue("#entry-to") };
  await sheetFits(page);
  // The narrowest phones still in use.
  await page.setViewportSize({ width: 320, height: 640 });
  await sheetFits(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await shot(page, "09-phone-entry-form", false);
  await submit(page);
  await sheetGone(page);
  await waitForNames(page, [...demoNames, "Zia Carla"]);
  const carlaItem = page.locator(".people .entry", { hasText: "Zia Carla" });
  assert.match(await carlaItem.innerText(), /segnato da Luca/);
  assert.match(await carlaItem.innerText(), /arriva in treno/);
  assert.equal(await page.locator('.months .bar.family[data-stay^="family:e_"]').count() >= 1, true, "the new period is drawn in the calendar");
  await barsFollowTheHouseHours(page);
  await noSidewaysScroll(page);
  await page.reload();
  await page.waitForSelector(".shell");
  await waitForNames(page, [...demoNames, "Zia Carla"]);

  // Dates with guests, or already taken by the family, are refused with the reason.
  await openAddForm(page);
  assert.equal(await page.inputValue("#entry-me"), "Luca", "the device remembers who is writing");
  assert.notEqual(await page.inputValue("#entry-from"), carla.from, "the next proposal skips the nights just taken");
  await page.fill("#entry-who", "Qualcuno");
  await page.fill("#entry-from", carla.from);
  await page.fill("#entry-to", carla.to);
  assert.match(await page.locator(".preview").innerText(), /C'è già un periodo in quelle date: Zia Carla/);
  await submit(page);
  assert.match(await page.locator("dialog.sheet .error").innerText(), /C'è già un periodo in quelle date/);
  const guests = await page.evaluate(() => {
    const bar = document.querySelector(".months .lane.p0 .bar.booking:not(.past):not(.cut-l)");
    const ymd = (day) => new Date(day * 86400000).toISOString().slice(0, 10);
    return { from: ymd(Number(bar.dataset.from)), to: ymd(Number(bar.dataset.from) + 1) };
  });
  await page.fill("#entry-from", guests.from);
  await page.fill("#entry-to", guests.to);
  assert.match(await page.locator(".preview").innerText(), /In quelle date ci sono ospiti/);
  await submit(page);
  assert.match(await page.locator("dialog.sheet .error").innerText(), /In quelle date ci sono ospiti/);
  // Moving the check-in past the check-out keeps one night.
  await page.fill("#entry-from", "2027-03-10");
  await page.fill("#entry-to", "2027-03-12");
  await page.fill("#entry-from", "2027-03-20");
  assert.equal(await page.inputValue("#entry-to"), "2027-03-21");
  await closeSheet(page);
  await waitForNames(page, [...demoNames, "Zia Carla"]);

  // Change it, then remove it: removal asks twice.
  await openEditForm(page, "Zia Carla");
  assert.match(await page.locator("dialog.sheet .sheet-head h2").innerText(), /Modifica periodo/);
  await page.fill("#entry-who", "Zia Carla e Piero");
  await submit(page);
  await sheetGone(page);
  await waitForNames(page, [...demoNames, "Zia Carla e Piero"]);
  await openEditForm(page, "Zia Carla e Piero");
  const remove = page.locator("dialog.sheet .btn.danger");
  await press(page, remove);
  assert.equal(await remove.innerText(), "Conferma eliminazione");
  assert.equal((await names(page)).length, demoNames.length + 1, "one click removes nothing");
  await press(page, remove);
  await sheetGone(page);
  await waitForNames(page, demoNames);
  await page.reload();
  await page.waitForSelector(".shell");
  await waitForNames(page, demoNames);
  await context.close();
  server.close();

  /* ---- a house whose portal could not be read: its family notes stay, unverified ---- */
  const missingDir = fs.mkdtempSync(path.join(os.tmpdir(), "casecal-e2e-"));
  execFileSync("python3", ["-m", "casecal", "demo", "--out", missingDir, "--variant", "missing"], { cwd: ROOT, stdio: "ignore" });
  const missingServer = await serve(missingDir);
  ({ context, page } = await open({ viewport: { width: 1280, height: 900 }, colorScheme: "light", deviceScaleFactor: SCALE },
    `http://127.0.0.1:${missingServer.address().port}/`));
  await unlock(page, FAMILY);
  assert.equal(await page.locator(".months .lane.p1").count(), 0, "a house that was not read has no calendar");
  const unread = page.locator(".people .entry", { hasText: "Nonna Ada" });
  assert.match(await unread.innerText(), /Non verificabile sul portale\s+il portale di questa casa non è stato letto/);
  await openEditForm(page, "Nonna Ada");
  assert.equal(await page.inputValue("#entry-house"), "lago", "its notes can still be changed");
  assert.match(await page.locator(".preview").innerText(), /Non verificabile: il portale di questa casa non è stato letto/);
  await closeSheet(page);
  await context.close();
  missingServer.close();
  fs.rmSync(missingDir, { recursive: true, force: true });

  /* ---- two devices on the shared log: GitHub is played by a stand-in ---- */
  const logDir = fs.mkdtempSync(path.join(os.tmpdir(), "casecal-e2e-"));
  execFileSync("python3", ["-c", BUILD_LOG_PAGE, logDir], { cwd: ROOT, stdio: ["ignore", "ignore", "inherit"] });
  const logServer = await serve(logDir);
  const logUrl = `http://127.0.0.1:${logServer.address().port}/`;
  const github = fakeGitHub();

  const phone = await open({ ...devices["iPhone 13"], colorScheme: "light" }, logUrl, github);
  await unlock(phone.page, FAMILY);
  await phone.page.waitForFunction(() => /Aggiornato con le modifiche di tutti/.test(document.querySelector(".people").textContent));
  await waitForNames(phone.page, demoNames);
  const firstCall = github.calls[0];
  assert.equal(firstCall.method + " " + firstCall.path, "GET " + LOG_PATH);
  assert.match(firstCall.query, /since=2026-01-01T00%3A00%3A00Z/, "the page asks only for what is newer than the nightly build");
  assert.equal(firstCall.headers.authorization, "Bearer " + TOKEN);

  await addPeriod(phone.page, "Marco e Giulia", "Marco", "con il cane");
  await waitForNames(phone.page, [...demoNames, "Marco e Giulia"]);
  assert.equal(github.comments.length, 1, "one change, one comment");
  assert.match(github.comments[0].body, /^casecal:1:[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$/);
  for (const secret of ["Marco", "Giulia", "cane", "mare", "lago"]) {
    assert.ok(!github.comments[0].body.includes(secret), "what GitHub stores is ciphertext");
  }

  // A second device opens the page later: the baked entries plus what the log holds.
  const desk = await open({ viewport: { width: 1280, height: 900 }, colorScheme: "dark", deviceScaleFactor: SCALE }, logUrl, github);
  await unlock(desk.page, FAMILY);
  await waitForNames(desk.page, [...demoNames, "Marco e Giulia"]);
  assert.match(await desk.page.locator(".people .entry", { hasText: "Marco e Giulia" }).innerText(), /segnato da Marco[\s\S]*con il cane/);

  // It changes the period; the first device sees the change when its tab is looked at again.
  await openEditForm(desk.page, "Marco e Giulia");
  await desk.page.fill("#entry-who", "Marco, Giulia e Tommaso");
  await desk.page.fill("#entry-me", "Giulia");
  await sheetFits(desk.page);
  await submit(desk.page);
  await sheetGone(desk.page);
  await waitForNames(desk.page, [...demoNames, "Marco, Giulia e Tommaso"]);
  await comeBack(phone.page);
  await waitForNames(phone.page, [...demoNames, "Marco, Giulia e Tommaso"]);
  assert.match(await phone.page.locator(".people .entry", { hasText: "Tommaso" }).innerText(), /segnato da Giulia/);
  await shot(desk.page, "10-shared-log-desktop-dark");

  // Each adds a period without having seen the other's: both are kept.
  await addPeriod(phone.page, "Nonno Piero", "Marco");
  await comeBack(desk.page);
  await waitForNames(desk.page, [...demoNames, "Marco, Giulia e Tommaso", "Nonno Piero"]);
  await addPeriod(desk.page, "Zia Carla", "Giulia");
  await comeBack(phone.page);
  await waitForNames(phone.page, [...demoNames, "Marco, Giulia e Tommaso", "Nonno Piero", "Zia Carla"]);

  // The first device removes the first period; it goes away on the second too.
  await openEditForm(phone.page, "Marco, Giulia e Tommaso");
  await press(phone.page, "dialog.sheet .btn.danger");
  await press(phone.page, "dialog.sheet .btn.danger");
  await sheetGone(phone.page);
  await waitForNames(phone.page, [...demoNames, "Nonno Piero", "Zia Carla"]);
  await comeBack(desk.page);
  await waitForNames(desk.page, [...demoNames, "Nonno Piero", "Zia Carla"]);
  for (const call of github.calls) {
    assert.equal(call.path, LOG_PATH, "the page talks to the log's comments and to nothing else");
    assert.ok(!("x-github-api-version" in call.headers), "no header outside those GitHub accepts from a browser");
  }

  // A token that stopped working: what is known stays on screen, the reason is said, nothing is lost.
  apiFailuresExpected = true;
  github.failWith = 401;
  await comeBack(phone.page);
  await phone.page.waitForFunction(() => /Modifiche recenti non lette/.test(document.querySelector(".people").textContent));
  assert.match(await syncText(phone.page), /token del registro di famiglia è scaduto o è stato revocato/);
  await waitForNames(phone.page, [...demoNames, "Nonno Piero", "Zia Carla"]);
  await openAddForm(phone.page);
  await phone.page.fill("#entry-who", "Non salvato");
  await submit(phone.page);
  await phone.page.waitForSelector("dialog.sheet .error:not([hidden])");
  assert.match(await phone.page.locator("dialog.sheet .error").innerText(), /token del registro di famiglia/);
  assert.equal(await phone.page.locator("dialog.sheet[open] form").count(), 1, "the form stays open with what was typed");
  assert.equal(await phone.page.inputValue("#entry-who"), "Non salvato");
  await sheetFits(phone.page);
  await shot(phone.page, "11-phone-token-error", false);
  // No connection at all: same form, the other reason; then the connection is back and it saves.
  github.failWith = "offline";
  await submit(phone.page);
  await phone.page.waitForFunction(() => /Connessione assente/.test(document.querySelector("dialog.sheet .error").textContent));
  github.failWith = null;
  await phone.page.fill("#entry-who", "Salvato al terzo tentativo");
  await submit(phone.page);
  await sheetGone(phone.page);
  await waitForNames(phone.page, [...demoNames, "Nonno Piero", "Zia Carla", "Salvato al terzo tentativo"]);
  await phone.page.waitForFunction(() => /Aggiornato con le modifiche di tutti/.test(document.querySelector(".people").textContent));
  // The other device was told to retry by its user.
  github.failWith = 403;
  await comeBack(desk.page);
  await desk.page.waitForFunction(() => /non ha più i permessi necessari/.test(document.querySelector(".people").textContent));
  github.failWith = null;
  await press(desk.page, '[data-key="sync-retry"]');
  await waitForNames(desk.page, [...demoNames, "Nonno Piero", "Zia Carla", "Salvato al terzo tentativo"]);
  apiFailuresExpected = false;
  await phone.context.close();
  await desk.context.close();
  logServer.close();

  // What the nightly job will make of those comments: the same list the page showed.
  const dump = path.join(logDir, "comments.json");
  fs.writeFileSync(dump, JSON.stringify(github.comments));
  const read = JSON.parse(execFileSync("python3", ["-c", READ_LOG_BACK, dump], { cwd: ROOT, encoding: "utf8" }));
  assert.deepEqual(read.kinds, ["set", "set", "set", "set", "del", "set"]);
  assert.deepEqual(read.entries, [["Nonno Piero", "Marco", ""], ["Salvato al terzo tentativo", "Marco", ""], ["Zia Carla", "Giulia", ""]]);
  fs.rmSync(logDir, { recursive: true, force: true });

  await browser.close();
  assert.deepEqual(problems, [], "console errors or external requests");
  console.log("smoke test passed");
}

main().catch((error) => { console.error(error); process.exit(1); });
