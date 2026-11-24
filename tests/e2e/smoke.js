"use strict";
/* End-to-end smoke test of the built page in a real browser.
 *
 *   python -m casecal demo --out dist-demo
 *   node tests/e2e/smoke.js dist-demo [screenshot-dir]
 *
 * Needs Playwright with Chromium; the CI workflow runs it on every push. It fails on
 * any console error, which is how a Content-Security-Policy violation would show up.
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
const PASSWORD = process.env.E2E_PASSWORD || "demo-calendario-delle-case"; // one password opens everything
const SCALE = Number(process.env.E2E_SCALE || 1);
const API = "https://api.github.com";
const LOG_PATH = "/repos/owner/repo/issues/7/comments";
const TOKEN = "github_pat_E2E";

/* The demo page again, but with the settings the nightly job writes when the family log is on. */
const BUILD_LOG_PAGE = `
import sys
from datetime import datetime, timezone
from casecal.demo import DEMO_PASSPHRASE, demo_snapshot
from casecal.sync import publish
snapshot = demo_snapshot(datetime.now(timezone.utc).replace(microsecond=0))
snapshot.log = {"kind": "github", "api": "${API}", "repo": "owner/repo", "issue": 7,
                "token": "${TOKEN}", "cursor": 1000, "since": "2026-01-01T00:00:00Z"}
publish(snapshot, passphrase=DEMO_PASSPHRASE, site_id="casecal-demo", out_dir=sys.argv[1])
`;
/* What the nightly job would make of the comments the page wrote. */
const READ_LOG_BACK = `
import json, sys
from casecal.crypto import derive_key, site_salt
from casecal.demo import DEMO_PASSPHRASE
from casecal.family import decode_event, fold
key = derive_key(DEMO_PASSPHRASE, site_salt("casecal-demo"))
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
  async function barsFollowTheHouseHours(page, needs = {}) {
    const report = await page.evaluate(() => {
      const IN = 15 / 24, OUT = 11 / 24;
      const result = { starts: 0, ends: 0, worst: 0, clipped: [], turnovers: 0, joins: 0, apart: [] };
      const rowOf = (bar) => bar.closest(".week");
      const has = (bar, name) => bar.classList.contains(name);
      for (const bar of document.querySelectorAll(".months .bar")) {
        const box = bar.getBoundingClientRect();
        const tile = (day) => rowOf(bar).querySelector(`.day[data-day="${day}"]`);
        // An edge where a family joins or leaves another is not a check-in or a check-out of the whole bar.
        if (!has(bar, "cut-l") && !has(bar, "join-l")) {
          const first = tile(bar.dataset.from).getBoundingClientRect();
          result.worst = Math.max(result.worst, Math.abs(box.left - (first.left + IN * first.width)));
          result.starts++;
        }
        if (!has(bar, "cut-r") && !has(bar, "join-r")) {
          const last = tile(bar.dataset.to).getBoundingClientRect();
          result.worst = Math.max(result.worst, Math.abs(box.right - (last.left + OUT * last.width)));
          result.ends++;
        }
        // Such an edge must touch the stretch it continues into: one band, no gap, no overlap.
        if (has(bar, "join-l")) {
          const before = [...bar.parentElement.querySelectorAll(".bar.family.join-r")]
            .some((other) => Math.abs(other.getBoundingClientRect().right - box.left) < 0.6);
          if (!before) result.apart.push(bar.dataset.stay);
          result.joins++;
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
          if (has(bars[i], "join-l") || has(bars[i - 1], "join-r")) continue; // sharing, not a turnover
          const gap = bars[i].getBoundingClientRect().left - bars[i - 1].getBoundingClientRect().right;
          const width = lane.closest(".week").querySelector(".day").getBoundingClientRect().width;
          result.worst = Math.max(result.worst, Math.abs(gap - (IN - OUT) * width));
          result.turnovers++;
        }
      }
      return result;
    });
    assert.ok(report.starts > 5 && report.ends > 5, "bars with real starts and ends were measured");
    if (needs.turnover !== false) assert.ok(report.turnovers > 0, "the demo contains a same-day turnover");
    assert.ok(report.worst < 1, `a bar is ${report.worst.toFixed(2)}px away from its hour`);
    assert.deepEqual(report.clipped, [], "labels that do not fit must not be drawn");
    assert.deepEqual(report.apart, [], "stretches of a shared stay must touch");
    return report;
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

  async function noSidewaysScroll(page, where = "") {
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 0, `page scrolls sideways by ${overflow}px${where && " " + where}`);
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

  /** The page shows one house at a time: go to another one the way this device does. */
  async function showHouse(page, label) {
    await press(page, page.locator(".seg label", { hasText: new RegExp("^" + label + "$") }));
    await page.waitForFunction((wanted) => {
      const heading = document.querySelector(".house h3");
      return heading && heading.textContent === wanted;
    }, label);
  }
  const houseShown = (page) => page.locator(".house h3").innerText();
  /** The photo at the top of the house on show: what it is, whether it decoded, where it sits. */
  const housePhoto = (page) => page.locator(".house .house-photo img").evaluate((img) => {
    const box = (node) => { const r = node.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height }; };
    return {
      loaded: img.complete && img.naturalWidth > 0, natural: [img.naturalWidth, img.naturalHeight], alt: img.alt, src: img.src,
      box: box(img), card: box(img.closest(".house")), head: box(img.closest(".house").querySelector(".house-head")),
      fit: getComputedStyle(img).objectFit
    };
  });
  const figures = (page) => page.locator("section.card", { hasText: "Area proprietà" });

  /* ---- helpers for the profit and loss ---- */
  /** The tab after the houses: the profit and loss of all of them. */
  async function showPnl(page) {
    await press(page, page.locator(".seg label", { hasText: /^P&L$/ }));
    await page.waitForSelector("section.pnl .statement");
  }
  /** "18.235 €", "226,90", "38,1%" -> a number. */
  const amountOf = (text) => Number(text.replace(/[^\d,-]/g, "").replace(",", "."));
  /**
   * The statement as numbers. `rows` maps each line to its cells (a house each, then
   * all of them), null where a cell holds no figure; `order` lists [part, line].
   */
  const readStatement = (page) => page.evaluate(() => {
    const own = (cell) => [...cell.childNodes].filter((node) => node.nodeType === Node.TEXT_NODE)
      .map((node) => node.textContent).join("").trim();
    const number = (text) => (/\d/.test(text) ? Number(text.replace(/[^\d,-]/g, "").replace(",", ".")) : null);
    const rows = {}, order = [];
    let part = "";
    for (const row of document.querySelectorAll(".pnl .statement tbody tr")) {
      if (row.classList.contains("part")) { part = row.textContent; continue; }
      const label = own(row.querySelector("th"));
      rows[label] = [...row.querySelectorAll("td")].map((cell) => number(own(cell)));
      order.push([part, label]);
    }
    return { rows, order };
  });
  /** The chart of the costs as drawn: a bar per cost, a segment per house that bears it. */
  const readCostBars = (page) => page.locator(".pnl .cost").evaluateAll((nodes) => nodes.map((node) => {
    const track = node.querySelector(".track").getBoundingClientRect();
    const segments = [...node.querySelectorAll(".seg-bar")].map((segment) => segment.getBoundingClientRect());
    return {
      label: [...node.querySelector(".cost-name").childNodes].filter((child) => child.nodeType === Node.TEXT_NODE)
        .map((child) => child.textContent).join(""),
      value: node.querySelector(".cost-value").firstChild.textContent,
      drawn: segments.reduce((sum, box) => sum + box.width, 0) / track.width,
      thickness: Math.max(...segments.map((box) => box.height)),
      segments: segments.length,
      inside: segments.every((box) => box.left >= track.left - 0.5 && box.right <= track.right + 0.5)
    };
  }));

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
  /** Who the open form says uses the house: the family chosen, or what is written under "Altro". */
  async function formWho(page) {
    const chosen = await page.locator(".choices input:checked + span").allTextContents();
    return chosen.length && chosen[0] !== "Altro" ? chosen[0] : page.inputValue("#entry-who");
  }
  /** Say who uses the house: one of the families on offer, or anyone else under "Altro". */
  async function chooseWho(page, who) {
    const family = page.locator(".choices label", { hasText: new RegExp("^" + who.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "$") });
    if (await family.count()) return press(page, family);
    if (await page.locator("#entry-who").isHidden()) await press(page, page.locator(".choices label", { hasText: /^Altro$/ }));
    await page.fill("#entry-who", who);
  }
  /** Open the form of the listed period that shows `text` (a name, or a note when names repeat). */
  async function openEditForm(page, text, who = text) {
    await press(page, page.locator(".people .entry", { hasText: text }).locator("button"));
    await page.waitForSelector("dialog.sheet[open] form");
    assert.equal(await formWho(page), who);
  }
  const submit = (page) => press(page, "dialog.sheet button[type=submit]");
  /** Add a period on the dates the form proposes; returns them. */
  async function addPeriod(page, who, me, note = "") {
    await openAddForm(page);
    assert.equal(await page.locator(".preview .ico-critical").count(), 0, "the proposed dates can be saved as they are");
    const dates = { from: await page.inputValue("#entry-from"), to: await page.inputValue("#entry-to") };
    await chooseWho(page, who);
    if (note) await page.fill("#entry-note", note);
    await page.fill("#entry-me", me);
    await submit(page);
    await sheetGone(page);
    return dates;
  }
  /** Make the page read the log again, as it does when its tab comes back into view. */
  const comeBack = (page) => page.evaluate(() => { document.dispatchEvent(new Event("visibilitychange")); });
  const syncText = (page) => page.locator(".people").innerText();

  /* ---- desktop, light: one password opens the calendar and the figures, one house at a time ---- */
  let { context, page } = await open({ viewport: { width: 1280, height: 900 }, colorScheme: "light", deviceScaleFactor: SCALE });
  await page.waitForSelector(".lock-card");
  assert.equal(await page.locator(".shell").count(), 0, "nothing is shown before unlocking");
  assert.equal(await page.locator("input[type=password]").count(), 1, "one password is asked for");
  await shot(page, "01-lock", false);

  await page.fill("#pw", "non-e-la-password-giusta");
  await page.click("button[type=submit]");
  await page.waitForSelector(".error:not([hidden])");
  assert.match(await page.textContent(".error"), /non corretta/);

  await page.fill("#pw", PASSWORD);
  await page.click("button[type=submit]");
  await page.waitForSelector(".shell");
  // A tab for each house and none for all of them together; the first house is on show.
  // After the houses, the profit and loss: the demo knows what they cost.
  assert.deepEqual(await page.locator(".seg input[name=house] + span").allInnerTexts(), ["Casa Mare", "Casa Lago", "P&L"]);
  assert.equal(await houseShown(page), "Casa Mare");
  assert.equal(await page.locator(".house").count(), 1);
  assert.equal(await page.locator(".months.single").count(), 1);
  // Which house is which can be seen before anything is read: each tab carries the house's
  // photo, ringed in its colour, and the house on show has it at the top of its card.
  const thumbs = await page.locator(".seg input[name=house] + span").evaluateAll((faces) => faces.map((face) => {
    const img = face.querySelector("img.thumb");
    return img && { loaded: img.complete && img.naturalWidth > 0, src: img.src, alt: img.alt, size: [img.offsetWidth, img.offsetHeight],
      ring: getComputedStyle(img).boxShadow };
  }));
  assert.equal(thumbs[2], null, "the P&L is not a house: no photo");
  assert.ok(thumbs[0].loaded && thumbs[1].loaded, "the photos on the tabs decode");
  assert.ok(thumbs[0].src.startsWith("data:image/webp;base64,") && thumbs[0].src !== thumbs[1].src, "each house has its own");
  assert.deepEqual([thumbs[0].size, thumbs[1].size], [[22, 22], [22, 22]]);
  assert.deepEqual([thumbs[0].alt, thumbs[1].alt], ["", ""], "the name beside the photo already says which house");
  assert.match(thumbs[0].ring, /rgb\(98, 80, 214\)/);
  assert.match(thumbs[1].ring, /rgb\(235, 104, 52\)/);
  assert.equal(await page.locator(".seg .swatch").count(), 0, "the photo takes the place of the plain swatch");
  const seaPhoto = await housePhoto(page);
  assert.ok(seaPhoto.loaded, "the photo of the house decodes");
  assert.deepEqual([seaPhoto.alt, seaPhoto.natural, seaPhoto.fit], ["Foto di Casa Mare", [840, 560], "cover"]);
  assert.equal(seaPhoto.src, thumbs[0].src, "the same picture as on the tab");
  // On a wide screen it stands beside the guests, inside the card.
  assert.ok(seaPhoto.box.left >= seaPhoto.card.left && seaPhoto.box.right <= seaPhoto.head.left, "photo beside the house's name");
  assert.ok(Math.abs(seaPhoto.box.width / seaPhoto.box.height - 1.5) < 0.02 && seaPhoto.box.bottom <= seaPhoto.card.bottom);
  assert.ok(!/Tutte/.test(await page.locator("#app").innerText()), "no view of all the houses together");
  // No second lock: the figures open with the same password.
  assert.equal(await page.locator("input[type=password]").count(), 0, "no second password inside the page");
  assert.equal(await page.locator(".tiles").count(), 1);
  assert.ok((await page.textContent("#app")).includes("Netto proprietario"), "the figures are part of the page");
  assert.ok(!/Chiudi area|seconda password/.test(await page.textContent("#app")));

  assert.ok((await page.locator(".month").count()) >= 2, "months rendered");
  assert.ok((await page.locator(".months .bar").count()) > 5, "stays drawn as bars");
  assert.match(await page.textContent(".hours"), /Check-in dalle 15:00, check-out entro le 11:00/);
  assert.deepEqual(await page.locator(".keyart .cap").allTextContents(), ["15:00", "11:00"]);
  await barsFollowTheHouseHours(page);
  const labels = await page.locator(".months .bar .txt").allTextContents();
  assert.ok(labels.some((text) => /ospiti/.test(text)), "guest counts written inside bars");
  assert.match(await page.textContent(".stays thead"), /Check-indalle 15:00Check-outentro le 11:00/);
  assert.equal(await page.locator(".day.today").count(), 1);
  await noSidewaysScroll(page);
  await tablesKeepTheirShape(page, 3); // guests to come, stays and monthly statistics of the house

  // The card of the house says who is there and who is coming next: no free periods, no blocked dates.
  const card = await page.locator(".house").innerText();
  assert.match(card, /PROSSIMI OSPITI/i);
  assert.ok(!/stanotte|periodi liberi|Bloccata|Libera/i.test(card), "the card lists guests only");
  const guestRows = page.locator(".house tbody tr");
  assert.deepEqual(await page.locator(".house thead th").evaluateAll((cells) => cells.map((cell) => cell.firstChild.textContent)),
    ["Check-in", "Check-out", "Notti", "Canale"]);
  assert.equal(await guestRows.count(), 4, "the first few stays are shown");
  // The demo has guests in the first house today and a same-day turnover in three days.
  assert.match(await guestRows.nth(0).innerText(), /in casa ora/);
  assert.equal(await page.locator(".house tbody tr.current").count(), 1);
  assert.match(await guestRows.nth(1).innerText(), /tra 3 giorni/);
  assert.equal(await page.locator(".house tbody .sub", { hasText: /^(tra \d+ giorni|oggi|domani)$/ }).count(), 1, "only the next arrival says how far it is");
  const allGuests = page.locator('[data-key="guests:mare"]');
  assert.match(await allGuests.innerText(), /^Mostra tutti i \d+ soggiorni$/);
  await allGuests.click();
  assert.ok((await guestRows.count()) > 4, "the whole list on request");
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute("data-key")), "guests:mare", "focus stays on the toggle");
  await tablesKeepTheirShape(page, 3);
  await page.locator('[data-key="guests:mare"]').click();
  assert.equal(await guestRows.count(), 4);

  // The first house is violet, the second orange (the ring around the photo on its tab says so
  // even while the first is on show); text on a solid bar is white on the light surface.
  const colour = (selector, property) => page.evaluate(([chosen, wanted]) => getComputedStyle(document.querySelector(chosen))[wanted], [selector, property]);
  const ringOf = (shadow) => shadow.slice(0, shadow.indexOf(")") + 1); // "rgb(...) 0px 0px 0px 2px" -> its colour
  assert.deepEqual([await colour(".house.p0 .swatch", "backgroundColor"), ringOf(await colour(".seg .p1 .thumb", "boxShadow")),
    await colour(".lane.p0 .bar.booking", "backgroundColor"), await colour(".lane.p0 .bar.booking", "color")],
  ["rgb(98, 80, 214)", "rgb(235, 104, 52)", "rgb(98, 80, 214)", "rgb(255, 255, 255)"]);

  // The family's own periods of this house: listed, drawn over the calendar, each with its standing on the portal.
  // The demo: two families share some blocked nights at the first house, and one of them
  // has a period at the second house on nights still for sale ("con gli zii").
  const demoNames = await names(page);
  assert.deepEqual(sorted(demoNames), ["Famiglia Bianchi", "Famiglia Rossi"]);
  assert.deepEqual(await page.locator(".people .entry-state strong").allTextContents(), ["Bloccata sul portale", "Bloccata sul portale"]);
  assert.ok((await page.locator(".months .bar.family.blocked").count()) >= 1, "a protected family period is drawn");
  const familyLabels = await page.locator(".months .bar.family .txt").allTextContents();
  assert.ok(familyLabels.some((text) => /Rossi/.test(text)), "family bars carry the name");
  // The nights the two families share are one bar with both names, touching the bars on either side.
  const sharedBar = page.locator('.months .bar.family[data-stay*="+"]');
  assert.ok((await sharedBar.count()) >= 1, "shared nights are drawn as one bar");
  // Where a piece of it has room for words, that is: on days when the shared nights fall across
  // the end of a week the bar is drawn in two short pieces, which carry the initials alone.
  const sharedWords = (await sharedBar.locator(".txt").allTextContents()).filter((text) => text.trim());
  assert.ok(!sharedWords.length || sharedWords.some((text) => /Rossi \+ (Famiglia )?Bianchi/.test(text)), "with both names");
  assert.ok((await barsFollowTheHouseHours(page)).joins >= 2, "and its two edges were measured");
  assert.equal(await page.locator(".todo").count(), 0, "periods already blocked need no action");
  assert.match(await syncText(page), /Versione dimostrativa: le modifiche restano su questo dispositivo/);
  await shot(page, "02-first-house-desktop");

  await page.hover(".day.today");
  await page.waitForSelector(".tip:not([hidden])");
  assert.equal(await page.locator(".tip .tip-row").count(), 1, "the readout is about the house on show");
  const mark = await page.locator(".tip .tip-row .dash").evaluate((node) => {
    const box = node.getBoundingClientRect();
    return [Math.round(box.width), Math.round(box.height)];
  });
  assert.deepEqual(mark, [14, 3], "the row carries the house's mark");
  assert.match(await page.textContent(".tip"), /Ospiti · Casa Maredalle 15:00 di .+ alle 11:00 di /);
  assert.ok(!(await page.textContent(".tip")).includes("Casa Lago"));
  await shot(page, "03-tooltip", false);
  const sharedDay = await sharedBar.first().getAttribute("data-from");
  await page.hover(`.day[data-day="${sharedDay}"]`);
  await page.waitForFunction(() => /Uso famiglia: Famiglia Rossi; Uso famiglia: Famiglia Bianchi/.test(document.querySelector(".tip").textContent));

  // Keyboard: arrows walk the days, Enter opens the day's sheet, Escape gives the focus back.
  await page.focus(".day.today");
  const before = await page.evaluate(() => document.activeElement.getAttribute("data-day"));
  await page.keyboard.press("ArrowRight");
  const after = await page.evaluate(() => document.activeElement.getAttribute("data-day"));
  assert.equal(Number(after), Number(before) + 1, "arrow keys walk the days");
  const spoken = await page.evaluate(() => document.activeElement.getAttribute("aria-label"));
  assert.match(spoken, /Casa Mare: /);
  assert.ok(!spoken.includes("Casa Lago"), "a day speaks of the house on show");
  await page.keyboard.press("Enter");
  await page.waitForSelector("dialog.sheet[open]");
  assert.equal(await page.locator("dialog.sheet .sheet-house").count(), 1, "the day's sheet is about the house on show");
  assert.equal(await page.locator(".tip:not([hidden])").count(), 0, "the readout makes way for the sheet");
  await sheetFits(page);
  await shot(page, "04-day-sheet", false);
  await page.keyboard.press("Escape");
  await sheetGone(page);
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute("data-day")), after, "focus returns to the day");

  // The figures of the house on show.
  assert.match(await figures(page).innerText(), /Casa Mare · anno \d{4}/);
  assert.equal(await page.locator(".tile").count(), 6);
  assert.equal(await page.locator(".kpi-tables h4", { hasText: "Statistiche mensili" }).count(), 1);
  const monthly = page.locator(".kpi-tables table").first();
  const headers = await monthly.locator("thead th").allTextContents();
  assert.deepEqual(headers.map((name) => name.replace(/\u00a0/g, " ")),
    ["Mese", "Notti", "Occ.", "ADR", "Lordo*", "Imposta", "Netto proprietario", "Netto su lordo*"]);
  const monthNames = await monthly.locator("tbody td:first-child").allTextContents();
  assert.deepEqual([monthNames.length, monthNames[0], monthNames[11]], [12, "Gennaio", "Dicembre"]);
  assert.match(await monthly.locator("tfoot").innerText(), /^Totale \d{4}\s/);
  // Its amounts are whole euros with the unit said once below: fewer characters, less to scroll on a phone.
  const monthlyCells = await monthly.locator("tbody td.num, tfoot td.num").allTextContents();
  assert.equal(monthlyCells.length, 13 * 7);
  for (const cell of monthlyCells) assert.match(cell, /^(\d{1,3}(\.\d{3})*|\d+%|–)$/, `"${cell}" is not a whole number`);
  assert.ok(monthlyCells.some((cell) => /^\d{1,3}\.\d{3}$/.test(cell)), "thousands are grouped");
  assert.match(await page.locator(".kpi-tables .hint").innerText(), /Importi in euro, arrotondati all'unità.*lordo = notti × ADR/);
  // Nothing else among the figures: the year's bookings are neither listed nor summed up
  // (what the houses earned and cost is the P&L's business).
  assert.equal(await page.locator(".kpi-tables > div").count(), 1);
  assert.equal(await page.locator(".kpi-tables table").count(), 1);
  assert.equal(await page.locator(".kpi-tables h4").count(), 1);
  assert.ok(!/Prenotazioni con check-in|Check-in|Check-out/.test(await page.locator(".kpi-tables").innerText()),
    "no booking is listed or totalled among the figures");
  assert.ok((await page.locator(".col").count()) > 0, "monthly chart drawn");
  await page.locator(".group").nth(7).hover();
  await page.waitForSelector(".tip:not([hidden])");
  await shot(page, "05-figures-desktop");
  await noSidewaysScroll(page);

  // The other house: its own hue, guests, family periods and figures, and nothing of the first.
  await showHouse(page, "Casa Lago");
  assert.equal(await page.locator(".house").count(), 1);
  assert.equal(await page.locator(".lane.p0").count(), 0);
  assert.ok((await page.locator(".lane.p1").count()) > 0, "the house keeps its own hue");
  assert.equal(await colour(".house.p1 .swatch", "backgroundColor"), "rgb(235, 104, 52)");
  const lakePhoto = await housePhoto(page);
  assert.ok(lakePhoto.loaded && lakePhoto.alt === "Foto di Casa Lago" && lakePhoto.src === thumbs[1].src, "the other house, the other photo");
  await barsFollowTheHouseHours(page, { turnover: false });
  assert.ok((await page.locator(".months .bar .txt").allTextContents()).includes("Bloccata"), "blocked periods labelled");
  assert.match(await page.locator(".house tbody tr").nth(0).innerText(), /tra 5 giorni/);
  assert.equal(await page.locator(".house tbody tr.current").count(), 0);
  assert.deepEqual(await names(page), ["Famiglia Bianchi"], "only its family periods are listed");
  assert.deepEqual(await page.locator(".people .entry-state strong").allTextContents(), ["Da far bloccare al gestore"]);
  assert.match(await page.locator(".people .entry", { hasText: "con gli zii" }).innerText(), /ancora in vendita: /);
  assert.ok((await page.locator(".months .bar.family.pending").count()) >= 1, "an unprotected period is drawn differently");
  // The nights still to be blocked for the family: this house has some, and says which.
  const todo = await page.locator(".todo").innerText();
  assert.match(todo, /Da far bloccare al gestore/);
  assert.match(todo, /Casa Lago\s+.+ → .+ · Famiglia Bianchi/);
  assert.ok(!todo.includes("Famiglia Rossi") && !todo.includes("Casa Mare"));
  await page.hover(".day.today");
  await page.waitForFunction(() => /Libera · Casa Lagodalle 11:00 \(check-out al mattino\)/.test(document.querySelector(".tip").textContent));
  const familyDay = await page.locator(".months .bar.family.pending").first().getAttribute("data-from");
  await page.hover(`.day[data-day="${familyDay}"]`);
  await page.waitForFunction(() => /Uso famiglia: Famiglia Bianchi \(da far bloccare al gestore\)/.test(document.querySelector(".tip").textContent));
  assert.match(await figures(page).innerText(), /Casa Lago · anno \d{4}/);
  assert.equal(await page.locator(".tile").count(), 6);
  await tablesKeepTheirShape(page, 3);
  await noSidewaysScroll(page);
  await shot(page, "06-second-house-desktop");

  // The profit and loss: what the houses earned, what they cost, what is left.
  await showPnl(page);
  assert.equal(await page.locator(".house, .months, .people").count(), 0, "the P&L takes the place of the house's own view");
  const pnl = page.locator("section.pnl");
  assert.deepEqual(await pnl.locator(".tile .label").allTextContents(),
    ["Ricavi netti", "Spese annue", "Margine", "Margine %", "Notti di pareggio"]);
  // The statement: a column per house and one for both.
  assert.deepEqual(await pnl.locator(".statement thead th").allTextContents(), ["Voce", "Casa Mare", "Casa Lago", "Totale"]);
  assert.deepEqual(await pnl.locator(".statement tr.part").allTextContents(), ["Entrate", "Spese", "Risultato"]);
  const { rows, order } = await readStatement(page);
  assert.deepEqual(order.filter(([part]) => part !== "Spese").map(([, label]) => label),
    ["ADR medio", "Notti vendute", "Ricavi lordi", "Trattenute", "Ricavi netti", "Margine", "Margine %", "Notti di pareggio"]);
  const costLabels = order.filter(([part, label]) => part === "Spese" && label !== "Totale spese").map(([, label]) => label);
  assert.ok(costLabels.length >= 10, "the costs are listed one by one");
  assert.equal(order[order.findIndex(([, label]) => label === "Totale spese") + 1][0], "Risultato", "their total closes the list");
  for (const label of costLabels) {
    const [mare, lago, both] = rows[label];
    assert.equal((mare || 0) + (lago || 0), both, `${label}: the total is the sum of the houses`);
  }
  assert.ok(costLabels.some((label) => rows[label][0] === null) && costLabels.some((label) => rows[label][1] === null),
    "a cost of one house only is blank for the other");
  assert.equal(await pnl.locator(".statement td", { hasText: /^n\.a\.$/ }).count(), 2);
  for (const column of [0, 1, 2]) {
    const listed = costLabels.reduce((sum, label) => sum + (rows[label][column] || 0), 0);
    assert.equal(rows["Totale spese"][column], listed, "the costs add up, column by column");
    // Each figure is rounded to the euro on its own: a difference of one is rounding, more is a mistake.
    assert.ok(Math.abs(rows["Ricavi lordi"][column] - rows["Trattenute"][column] - rows["Ricavi netti"][column]) <= 1);
    assert.ok(Math.abs(rows["Ricavi netti"][column] - rows["Totale spese"][column] - rows["Margine"][column]) <= 1);
    assert.ok(rows["Notti di pareggio"][column] > 0 && Number.isInteger(rows["Notti di pareggio"][column]));
  }
  assert.equal(rows["Notti vendute"][0] + rows["Notti vendute"][1], rows["Notti vendute"][2]);
  assert.ok(Math.abs(rows["Ricavi netti"][0] + rows["Ricavi netti"][1] - rows["Ricavi netti"][2]) <= 1);
  assert.ok(Math.abs(rows["Margine"][0] + rows["Margine"][1] - rows["Margine"][2]) <= 1);
  // The headline figures are the statement's column for both houses.
  const headline = await pnl.locator(".tile").evaluateAll((tiles) => Object.fromEntries(tiles.map((tile) =>
    [tile.querySelector(".label").textContent, tile.querySelector(".value").textContent])));
  assert.match(headline["Ricavi netti"], /^[\d.]+\s€$/);
  assert.deepEqual(
    ["Ricavi netti", "Spese annue", "Margine", "Margine %", "Notti di pareggio"].map((label) => amountOf(headline[label])),
    ["Ricavi netti", "Totale spese", "Margine", "Margine %", "Notti di pareggio"].map((label) => rows[label][2]));
  assert.match(await pnl.locator(".rounding").innerText(), /arrotondati all'euro/);
  // Where the costs go: a bar per cost, the largest first, split between the houses.
  const costBars = await readCostBars(page);
  assert.equal(costBars.length, costLabels.length);
  const drawnAmounts = costBars.map((bar) => amountOf(bar.value));
  assert.deepEqual(drawnAmounts, [...drawnAmounts].sort((a, b) => b - a), "the largest cost comes first");
  costBars.forEach((bar, index) => {
    assert.equal(drawnAmounts[index], rows[bar.label][2], `${bar.label}: the figure beside the bar is the statement's`);
    assert.ok(Math.abs(bar.drawn - drawnAmounts[index] / drawnAmounts[0]) < 0.02, `${bar.label}: the bar is as long as its share of the largest`);
    assert.equal(bar.segments, rows[bar.label].slice(0, 2).filter((value) => value > 0).length, `${bar.label}: a segment per house that bears it`);
    assert.ok(bar.inside && bar.thickness <= 24, `${bar.label}: a thin bar inside its track`);
  });
  assert.deepEqual(await pnl.locator(".chart .legend > span").allTextContents(), ["Casa Mare", "Casa Lago"]);
  assert.equal(await colour(".pnl .seg-bar.p0", "backgroundColor"), "rgb(98, 80, 214)");
  assert.equal(await colour(".pnl .seg-bar.p1", "backgroundColor"), "rgb(235, 104, 52)");
  await pnl.locator(".cost").first().hover();
  await page.waitForSelector(".tip:not([hidden])");
  const costTip = await page.locator(".tip").innerText();
  assert.ok(costTip.includes(costBars[0].label) && costTip.includes("Casa Mare") && costTip.includes("Casa Lago"));
  assert.match(costTip, /\d,\d{2}\s€/, "the readout keeps the cents the statement rounds away");
  await tablesKeepTheirShape(page, 1);
  await noSidewaysScroll(page);
  await shot(page, "06b-pnl-desktop");
  // Another year of the workbook: its own costs, against that year's bookings.
  const pnlYears = await pnl.locator(".seg input[name=pnl-year] + span").allInnerTexts();
  assert.equal(pnlYears.length, 2);
  assert.ok((await pnl.locator("figcaption").innerText()).endsWith(pnlYears[0]));
  await pnl.locator(".seg label", { hasText: pnlYears[1] }).click();
  await page.waitForFunction((year) => document.querySelector(".pnl figcaption").textContent.endsWith(year), pnlYears[1]);
  const nextYear = await readStatement(page);
  assert.ok(nextYear.rows["Totale spese"][2] > rows["Totale spese"][2], "the next year's sheet has costs of its own");
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute("data-key")), "pnl-year:" + pnlYears[1]);
  // The page opens on a house: the P&L is a place to go to, not where a device starts from.
  await page.reload();
  await page.waitForSelector(".shell");
  assert.equal(await page.locator(".pnl").count(), 0);
  assert.equal(await houseShown(page), "Casa Lago");

  await page.click("text=Mostra i mesi passati");
  assert.ok((await page.locator(".month.past").count()) >= 1 || new Date().getMonth() === 0);

  // The device remembers the key and the house it was showing.
  await page.reload();
  await page.waitForSelector(".shell");
  assert.equal(await page.locator(".lock-card").count(), 0, "the device remembers the key");
  assert.equal(await houseShown(page), "Casa Lago", "and the house it showed last");
  assert.equal(await page.locator(".tiles").count(), 1, "figures included");

  // A device that remembered its keys when the figures had a password of their own still
  // opens the page; the key of the figures alone opens nothing any more and is not kept.
  await page.evaluate(() => {
    const keys = JSON.parse(localStorage.getItem("casecal.keys.v1"));
    localStorage.setItem("casecal.keys.v1", JSON.stringify({ ...keys, owner: btoa("x".repeat(32)) }));
  });
  await page.reload();
  await page.waitForSelector(".shell");
  assert.equal(await page.locator(".tiles").count(), 1);
  assert.deepEqual(await page.evaluate(() => Object.keys(JSON.parse(localStorage.getItem("casecal.keys.v1"))).sort()),
    ["family", "salt"]);

  await page.click("text=Blocca");
  await page.waitForSelector(".lock-card");
  await page.reload();
  await page.waitForSelector(".lock-card");
  assert.equal(await page.locator(".shell").count(), 0, "locking forgets the key");
  await context.close();

  /* ---- phone, dark ---- */
  ({ context, page } = await open({ ...devices["iPhone 13"], colorScheme: "dark" }));
  await unlock(page, PASSWORD);
  await page.waitForSelector(".tiles");
  assert.equal(await houseShown(page), "Casa Mare", "a device that never chose starts from the first house");
  assert.equal(await page.locator(".house").count(), 1);
  // On a phone the photo is a band across the top of the card, edge to edge, above the house's name.
  const band = await housePhoto(page);
  assert.ok(band.loaded && band.alt === "Foto di Casa Mare");
  assert.ok(Math.abs(band.box.width - band.card.width) <= 2.5 && Math.abs(band.box.top - band.card.top) <= 1.5, "edge to edge");
  assert.ok(band.box.height >= 120 && band.box.height <= 180 && band.box.bottom <= band.head.top, "a band above the name");
  assert.equal(await page.locator(".seg img.thumb").count(), 2);
  await noSidewaysScroll(page);
  await tablesKeepTheirShape(page, 3);
  // On the dark surface the same two hues are lighter, and the text on a solid bar is dark.
  const darkColours = await page.evaluate(() => {
    const shade = (selector, property) => getComputedStyle(document.querySelector(selector))[property];
    const ring = shade(".seg .p1 .thumb", "boxShadow");
    return [shade(".house.p0 .swatch", "backgroundColor"), ring.slice(0, ring.indexOf(")") + 1),
      shade(".lane.p0 .bar.booking:not(.past)", "color")];
  });
  assert.deepEqual(darkColours, ["rgb(144, 133, 233)", "rgb(217, 89, 38)", "rgb(11, 11, 11)"]);
  await barsFollowTheHouseHours(page);
  await shot(page, "07-phone-dark");
  // Bars are narrower in landscape or after a resize: labels must be fitted again.
  await page.setViewportSize({ width: 844, height: 390 });
  await page.waitForTimeout(150);
  await barsFollowTheHouseHours(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(150);
  await barsFollowTheHouseHours(page);
  // Phones come in many widths and with fonts of their own, some wider than this
  // machine's. On none of them may the page scroll sideways: a table too wide for its
  // card scrolls inside its own box. From 390px up the guests and stays tables do not
  // need even that. (At 360 and 375px the stays table once pushed the page sideways,
  // unnoticed because only 390px was looked at.)
  const boxed = ".scroll > table.rows:not(.kpi)";
  for (const house of ["Casa Lago", "Casa Mare"]) {
    await showHouse(page, house);
    assert.equal(await page.locator(boxed).count(), 2, "guests and stays tables sit in a box that scrolls on its own");
    for (const width of [320, 344, 360, 375, 412, 430, 390]) {
      await page.setViewportSize({ width, height: 844 });
      await page.waitForTimeout(150);
      await noSidewaysScroll(page, `at ${width}px (${house})`);
      const cramped = await page.evaluate((selector) => [...document.querySelectorAll(selector)]
        .filter((table) => table.parentElement.scrollWidth > table.parentElement.clientWidth).length, boxed);
      if (width >= 390) assert.equal(cramped, 0, `at ${width}px a guests or stays table of ${house} needs sideways scrolling`);
    }
    // The monthly statistics have too many columns for a phone, but only just: whole
    // euros and headings that wrap keep the sideways scrolling short. (With two decimals
    // and a width fixed per column the first house's table was two screens wide.)
    const monthlyBox = await page.evaluate(() => {
      const box = document.querySelector(".kpi-tables .scroll");
      return { columns: box.querySelectorAll("thead th").length, beyond: box.scrollWidth - box.clientWidth, width: box.clientWidth };
    });
    const allowed = monthlyBox.columns <= 8 ? 0.5 : 1; // of a screen
    assert.ok(monthlyBox.beyond <= allowed * monthlyBox.width,
      `the monthly statistics of ${house} scroll sideways by ${monthlyBox.beyond}px on a phone`);
  }
  // The P&L on the same phones: three columns of figures side by side. From 360px up
  // the statement fits its card; below, it scrolls in its own box like the other tables.
  await showPnl(page);
  for (const width of [320, 344, 360, 375, 412, 430, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await page.waitForTimeout(150);
    await noSidewaysScroll(page, `at ${width}px (P&L)`);
    const beyond = await page.evaluate(() => {
      const box = document.querySelector(".pnl .scroll");
      return box.scrollWidth - box.clientWidth;
    });
    if (width >= 360) assert.ok(beyond <= 0, `at ${width}px the statement needs sideways scrolling (${beyond}px)`);
  }
  await tablesKeepTheirShape(page, 1);
  assert.equal(await colour(".pnl .seg-bar.p0", "backgroundColor"), "rgb(144, 133, 233)");
  // A tap on a cost opens its readout, house by house; a second tap closes it.
  const tapped = page.locator(".pnl .cost").nth(1);
  await tapped.tap();
  await page.waitForSelector(".tip:not([hidden])");
  const tappedTip = await page.locator(".tip").innerText();
  assert.ok(tappedTip.includes("Casa Mare") && tappedTip.includes("Casa Lago"));
  await tapped.tap();
  await page.waitForSelector(".tip", { state: "hidden" });
  await shot(page, "07b-phone-pnl-dark");
  await showHouse(page, "Casa Mare");
  // A tap on a day opens its sheet; no hover readout is left behind when it closes.
  await page.locator(".day.today").tap();
  await page.waitForSelector("dialog.sheet[open]");
  assert.match(await page.locator("dialog.sheet").innerText(), /Casa Mare\s+Ospiti · dalle 15:00/);
  assert.equal(await page.locator("dialog.sheet .sheet-house").count(), 1, "the sheet is about the house on show");
  await sheetFits(page);
  await shot(page, "08-phone-day-sheet", false);
  await closeSheet(page);
  assert.equal(await page.locator(".tip:not([hidden])").count(), 0);
  await context.close();

  /* ---- phone, light: the family adds, changes and removes its periods (demo: kept in the browser) ---- */
  ({ context, page } = await open({ ...devices["iPhone 13"], colorScheme: "light" }));
  await unlock(page, PASSWORD);
  const sharedBars = () => page.locator('.months .lane.p0 .bar.family[data-stay*="+"]').count();
  const sharedAtFirst = await sharedBars();
  // From a free day of the calendar, at the second house.
  await showHouse(page, "Casa Lago");
  const lakeDay = await page.evaluate(() => {
    // The day before the pending period at the lake house is free there, by construction of the demo.
    return Number(document.querySelector(".months .bar.family.pending").dataset.from) - 1;
  });
  await page.locator(`.day[data-day="${lakeDay}"]`).tap();
  await page.waitForSelector("dialog.sheet[open]");
  await page.locator("dialog.sheet .sheet-house", { hasText: "Casa Lago" }).locator("button", { hasText: "Segna un periodo da questo giorno" }).tap();
  await page.waitForSelector("dialog.sheet[open] form");
  assert.equal(await page.inputValue("#entry-house"), "lago");
  assert.equal(await page.evaluate(() => Math.round(Date.parse(document.querySelector("#entry-from").value) / 86400000)), lakeDay);
  // One night only: a proposal never lands on the nights another family already has.
  assert.equal(await page.evaluate(() => Math.round(Date.parse(document.querySelector("#entry-to").value) / 86400000)), lakeDay + 1);
  await closeSheet(page);

  // From the list, back at the first house: the form starts from the house on show. It offers the
  // families; nothing is saved until one is chosen and the writer is named.
  await showHouse(page, "Casa Mare");
  await openAddForm(page);
  assert.equal(await page.inputValue("#entry-house"), "mare");
  assert.deepEqual(await page.locator(".choices label").allTextContents(), ["Famiglia Rossi", "Famiglia Bianchi", "Altro"]);
  assert.equal(await page.locator(".choices input:checked").count(), 0, "a device that never wrote assumes no family");
  assert.ok(await page.locator("#entry-who").isHidden(), "free text only under Altro");
  assert.match(await page.locator(".preview").first().innerText(), /^Dalle 15:00 di .+ alle 11:00 di .+, \d+ nott[ei]\. /);
  await submit(page);
  await page.waitForSelector("dialog.sheet .error:not([hidden])");
  assert.match(await page.locator("dialog.sheet .error").innerText(), /Scegli chi usa la casa/);
  // Anyone who is not one of the families goes under "Altro".
  await chooseWho(page, "Zia Carla");
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
  assert.match(await carlaItem.innerText(), /Da far bloccare al gestore/);
  assert.equal(await page.locator('.months .bar.family[data-stay^="family:e_"]').count() >= 1, true, "the new period is drawn in the calendar");
  await barsFollowTheHouseHours(page);
  await noSidewaysScroll(page);
  await page.reload();
  await page.waitForSelector(".shell");
  await waitForNames(page, [...demoNames, "Zia Carla"]);

  // A family on the same nights: allowed, and said before saving.
  await openAddForm(page);
  assert.equal(await page.inputValue("#entry-me"), "Luca", "the device remembers who is writing");
  assert.notEqual(await page.inputValue("#entry-from"), carla.from, "the next proposal skips the nights just taken");
  await page.fill("#entry-from", carla.from);
  await page.fill("#entry-to", carla.to);
  await chooseWho(page, "Famiglia Rossi");
  assert.equal(await page.locator("dialog.sheet .preview .ico-critical").count(), 0, "sharing a house is not an error");
  const notices = await page.locator("dialog.sheet .preview").allInnerTexts();
  assert.ok(notices.some((text) => /In quelle date c'è anche Zia Carla \(.+ → .+\): la casa sarà condivisa\./.test(text)), notices.join(" | "));
  await page.fill("#entry-note", "prova condivisa");
  await sheetFits(page);
  await shot(page, "09b-phone-shared-period", false);
  await submit(page);
  await sheetGone(page);
  await waitForNames(page, [...demoNames, "Zia Carla", "Famiglia Rossi"]);
  assert.ok((await sharedBars()) > sharedAtFirst, "the nights in common are one bar for the two of them");
  await barsFollowTheHouseHours(page);

  // The device now starts from the family it wrote for; guests are still a refusal.
  await openAddForm(page);
  assert.deepEqual(await page.locator(".choices input:checked + span").allTextContents(), ["Famiglia Rossi"]);
  const guests = await page.evaluate(() => {
    const bar = document.querySelector(".months .lane.p0 .bar.booking:not(.past):not(.cut-l)");
    const ymd = (day) => new Date(day * 86400000).toISOString().slice(0, 10);
    return { from: ymd(Number(bar.dataset.from)), to: ymd(Number(bar.dataset.from) + 1) };
  });
  await page.fill("#entry-from", guests.from);
  await page.fill("#entry-to", guests.to);
  assert.match(await page.locator(".preview").first().innerText(), /In quelle date ci sono ospiti/);
  await submit(page);
  assert.match(await page.locator("dialog.sheet .error").innerText(), /In quelle date ci sono ospiti/);
  // Moving the check-in past the check-out keeps one night.
  await page.fill("#entry-from", "2027-03-10");
  await page.fill("#entry-to", "2027-03-12");
  await page.fill("#entry-from", "2027-03-20");
  assert.equal(await page.inputValue("#entry-to"), "2027-03-21");
  await closeSheet(page);
  await waitForNames(page, [...demoNames, "Zia Carla", "Famiglia Rossi"]);

  // Remove the shared period again: found by its note, removal asks twice.
  await openEditForm(page, "prova condivisa", "Famiglia Rossi");
  let remove = page.locator("dialog.sheet .btn.danger");
  await press(page, remove);
  assert.equal(await remove.innerText(), "Conferma eliminazione");
  assert.equal((await names(page)).length, demoNames.length + 2, "one click removes nothing");
  await press(page, remove);
  await sheetGone(page);
  await waitForNames(page, [...demoNames, "Zia Carla"]);
  assert.equal(await sharedBars(), sharedAtFirst);

  // Change the other one, then remove it too.
  await openEditForm(page, "Zia Carla");
  assert.match(await page.locator("dialog.sheet .sheet-head h2").innerText(), /Modifica periodo/);
  await page.fill("#entry-who", "Zia Carla e Piero");
  await submit(page);
  await sheetGone(page);
  await waitForNames(page, [...demoNames, "Zia Carla e Piero"]);
  // A period of "Altro" can be handed to one of the families, and back.
  await openEditForm(page, "Zia Carla e Piero");
  await chooseWho(page, "Famiglia Bianchi");
  assert.ok(await page.locator("#entry-who").isHidden());
  await submit(page);
  await sheetGone(page);
  await waitForNames(page, [...demoNames, "Famiglia Bianchi"]);
  await openEditForm(page, "arriva in treno", "Famiglia Bianchi");
  remove = page.locator("dialog.sheet .btn.danger");
  await press(page, remove);
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
  await unlock(page, PASSWORD);
  await showHouse(page, "Casa Lago"); // it keeps its tab
  assert.match(await page.locator(".house").innerText(), /Dati non disponibili/);
  assert.equal(await page.locator(".months .lane").count(), 0, "a house that was not read has no calendar");
  const unread = page.locator(".people .entry", { hasText: "con gli zii" });
  assert.match(await unread.innerText(), /Non verificabile sul portale\s+il portale di questa casa non è stato letto/);
  await openEditForm(page, "con gli zii", "Famiglia Bianchi");
  assert.equal(await page.inputValue("#entry-house"), "lago", "its notes can still be changed");
  assert.match(await page.locator(".preview").first().innerText(), /Non verificabile: il portale di questa casa non è stato letto/);
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
  await unlock(phone.page, PASSWORD);
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
  await unlock(desk.page, PASSWORD);
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
  await chooseWho(phone.page, "Non salvato");
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
