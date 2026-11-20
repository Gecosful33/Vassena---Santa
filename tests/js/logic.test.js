"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const L = require("../../casecal/web/logic.js");

const day = L.dayFromYmd;

function house(stays, from = "2026-01-01", to = "2028-01-01") {
  return L.prepare({
    id: "casa", label: "Casa", unit: null, fetched_at: "2026-06-01T02:00:00Z",
    from, to, stays
  });
}

test("day arithmetic is immune to daylight saving", () => {
  assert.equal(day("2026-03-30") - day("2026-03-28"), 2); // CET -> CEST weekend
  assert.equal(day("2026-10-26") - day("2026-10-24"), 2); // CEST -> CET weekend
  assert.equal(L.ymd(day("2028-02-29")), "2028-02-29");
  assert.deepEqual(L.parts(day("2026-08-15")), { y: 2026, m: 8, d: 15, dow: 5 }); // a Saturday
});

test("today is computed in the houses' time zone", () => {
  // 22:30 UTC on 31 Dec is already 1 Jan in Rome.
  assert.equal(L.todayInZone("Europe/Rome", Date.UTC(2026, 11, 31, 23, 30)), day("2027-01-01"));
  assert.equal(L.todayInZone("Europe/Rome", Date.UTC(2026, 7, 15, 21, 59)), day("2026-08-15"));
  assert.equal(L.todayInZone("Europe/Rome", Date.UTC(2026, 7, 15, 22, 0)), day("2026-08-16"));
});

test("a night is busy from check-in up to, not including, check-out", () => {
  const p = house([["2026-08-01", "2026-08-07", "b"]]);
  assert.equal(L.dayInfo(p, day("2026-07-31")).state, "free");
  assert.equal(L.dayInfo(p, day("2026-08-01")).state, "booking");
  assert.equal(L.dayInfo(p, day("2026-08-06")).state, "booking");
  const checkout = L.dayInfo(p, day("2026-08-07"));
  assert.equal(checkout.state, "free");
  assert.equal(checkout.leaving.end, day("2026-08-07"));
});

test("days outside the known data are unknown, never free", () => {
  const p = house([], "2026-01-01", "2027-01-01");
  assert.equal(L.dayInfo(p, day("2025-12-31")).state, "unknown");
  assert.equal(L.dayInfo(p, day("2027-01-01")).state, "unknown");
  assert.equal(L.nightStatus(p, day("2027-01-01")).state, "unknown");
});

test("a house that could not be read is unknown and has no free windows", () => {
  const p = L.prepare({ id: "x", label: "X", missing: true });
  assert.equal(L.dayInfo(p, day("2026-08-01")).state, "unknown");
  assert.deepEqual(L.freeWindows(p, day("2026-08-01")), []);
});

test("free windows run between stays and stay open at the end of the data", () => {
  const p = house([
    ["2026-08-01", "2026-08-07", "b"],
    ["2026-08-07", "2026-08-10", "b"], // same-day turnover
    ["2026-08-20", "2026-08-25", "c"]
  ], "2026-01-01", "2027-01-01");
  const windows = L.freeWindows(p, day("2026-07-30"));
  assert.deepEqual(windows.map((w) => [L.ymd(w.start), L.ymd(w.end), w.nights, w.open]), [
    ["2026-07-30", "2026-08-01", 2, false],
    ["2026-08-10", "2026-08-20", 10, false],
    ["2026-08-25", "2027-01-01", 129, true]
  ]);
});

test("free windows starting inside a stay begin when it ends", () => {
  const p = house([["2026-08-01", "2026-08-07", "b"]], "2026-01-01", "2026-09-01");
  const windows = L.freeWindows(p, day("2026-08-03"));
  assert.deepEqual(windows.map((w) => [L.ymd(w.start), L.ymd(w.end)]), [["2026-08-07", "2026-09-01"]]);
});

test("a fully booked remainder yields no free window", () => {
  const p = house([["2026-12-20", "2027-01-01", "b"]], "2026-01-01", "2027-01-01");
  assert.deepEqual(L.freeWindows(p, day("2026-12-24")), []);
});

test("night status follows back-to-back stays to the first free night", () => {
  const p = house([
    ["2026-08-01", "2026-08-07", "b"],
    ["2026-08-07", "2026-08-10", "c"],
    ["2026-08-12", "2026-08-14", "b"]
  ]);
  const busy = L.nightStatus(p, day("2026-08-02"));
  assert.equal(busy.state, "booking");
  assert.equal(L.ymd(busy.until), "2026-08-10");
  const free = L.nightStatus(p, day("2026-08-10"));
  assert.deepEqual([free.state, L.ymd(free.until), free.open], ["free", "2026-08-12", false]);
  const last = L.nightStatus(p, day("2026-08-14"));
  assert.deepEqual([last.state, L.ymd(last.until), last.open], ["free", "2028-01-01", true]);
});

test("busy until the end of the data reports no known free date", () => {
  const p = house([["2026-12-20", "2027-01-01", "b"]], "2026-01-01", "2027-01-01");
  assert.equal(L.nightStatus(p, day("2026-12-25")).until, null);
});

test("month weeks start on Monday and cover every day exactly once", () => {
  const weeks = L.monthWeeks(2026, 8); // 1 Aug 2026 is a Saturday
  assert.equal(weeks.length, 6);
  assert.deepEqual(weeks[0].map((d) => (d === null ? null : L.parts(d).d)), [null, null, null, null, null, 1, 2]);
  assert.deepEqual(weeks[5].map((d) => (d === null ? null : L.parts(d).d)), [31, null, null, null, null, null, null]);
  const all = weeks.flat().filter((d) => d !== null);
  assert.equal(all.length, 31);
  assert.equal(new Set(all).size, 31);
  // February 2027 starts on a Monday and fills exactly four rows.
  assert.equal(L.monthWeeks(2027, 2).length, 4);
});

test("months between spans year boundaries", () => {
  const months = L.monthsBetween(day("2026-11-20"), day("2027-03-01"));
  assert.deepEqual(months, [{ y: 2026, m: 11 }, { y: 2026, m: 12 }, { y: 2027, m: 1 }, { y: 2027, m: 2 }]);
  assert.deepEqual(L.monthsBetween(day("2026-11-20"), day("2026-11-20")), []);
});

/* ---- bars: from the check-in hour of the first day to the check-out hour of the last ---- */

const IN = 15 / 24, OUT = 11 / 24; // 15:00 and 11:00 as fractions of a day
const near = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} is not ${expected}`);

test("house hours default to 15:00 and 11:00 and can be configured", () => {
  const usual = house([]);
  assert.deepEqual([usual.hours.checkIn, usual.hours.checkOut], ["15:00", "11:00"]);
  near(usual.hours.inAt, 0.625);
  near(usual.hours.outAt, 11 / 24);
  const late = L.prepare({ id: "a", label: "A", fetched_at: "2026-06-01T02:00:00Z", from: "2026-01-01", to: "2027-01-01",
    stays: [], check_in: "16:30", check_out: "09:00" });
  assert.deepEqual([late.hours.checkIn, late.hours.checkOut], ["16:30", "09:00"]);
  near(late.hours.inAt, 16.5 / 24);
  near(late.hours.outAt, 9 / 24);
  // Anything unreadable falls back to the usual hours instead of drawing nonsense.
  const odd = L.prepare({ id: "a", label: "A", fetched_at: "2026-06-01T02:00:00Z", from: "2026-01-01", to: "2027-01-01",
    stays: [], check_in: "3pm", check_out: "25:00" });
  assert.deepEqual([odd.hours.checkIn, odd.hours.checkOut], ["15:00", "11:00"]);
  // A house that could not be read still has hours, so nothing breaks around it.
  assert.equal(L.prepare({ id: "x", label: "X", missing: true }).hours.checkIn, "15:00");
});

test("a bar starts at the check-in hour and ends at the check-out hour", () => {
  const p = house([["2026-08-04", "2026-08-06", "b"]]);
  const week = L.monthWeeks(2026, 8)[1]; // Mon 3 .. Sun 9
  const [segment] = L.weekSegments(p.stays, week, IN, OUT);
  near(segment.left, 1 + 15 / 24);  // Tuesday 4, 15:00
  near(segment.right, 3 + 11 / 24); // Thursday 6, 11:00
  assert.deepEqual([segment.cutLeft, segment.cutRight], [false, false]);
});

test("a one-night stay spans an afternoon, a night and a morning", () => {
  const p = house([["2026-08-04", "2026-08-05", "b"]]);
  const [segment] = L.weekSegments(p.stays, L.monthWeeks(2026, 8)[1], IN, OUT);
  near(segment.right - segment.left, 1 - 15 / 24 + 11 / 24); // 20 hours
});

test("a same-day turnover leaves the hours between check-out and check-in open", () => {
  const p = house([["2026-08-03", "2026-08-05", "b"], ["2026-08-05", "2026-08-08", "b"]]);
  const segments = L.weekSegments(p.stays, L.monthWeeks(2026, 8)[1], IN, OUT);
  assert.equal(segments.length, 2);
  near(segments[0].right, 2 + 11 / 24); // out by 11:00 on Wednesday 5
  near(segments[1].left, 2 + 15 / 24);  // in from 15:00 the same day
  near(segments[1].left - segments[0].right, 4 / 24); // four hours for the cleaning
});

test("bars are cut at week and month edges", () => {
  const p = house([["2026-05-30", "2026-06-01", "b"]]);
  const mayRows = L.monthWeeks(2026, 5);
  const [inMay] = L.weekSegments(p.stays, mayRows[mayRows.length - 1], IN, OUT); // Mon 25 .. Sun 31
  near(inMay.left, 5 + 15 / 24);
  assert.deepEqual([inMay.right, inMay.cutLeft, inMay.cutRight], [7, false, true]);
  const [inJune] = L.weekSegments(p.stays, L.monthWeeks(2026, 6)[0], IN, OUT); // Mon 1 June
  near(inJune.right, 11 / 24);
  assert.deepEqual([inJune.left, inJune.cutLeft, inJune.cutRight], [0, true, false]);
});

test("a stay that checks out on a Monday still shows its last morning", () => {
  const p = house([["2026-08-07", "2026-08-10", "b"]]); // Fri 7 -> Mon 10
  const [tail] = L.weekSegments(p.stays, L.monthWeeks(2026, 8)[2], IN, OUT); // Mon 10 .. Sun 16
  near(tail.right, 11 / 24);
  assert.deepEqual([tail.left, tail.cutLeft, tail.cutRight], [0, true, false]);
});

test("other house hours move the ends of the bar", () => {
  const p = house([["2026-08-04", "2026-08-06", "c"]]);
  const [segment] = L.weekSegments(p.stays, L.monthWeeks(2026, 8)[1], 17 / 24, 10 / 24);
  near(segment.left, 1 + 17 / 24);
  near(segment.right, 3 + 10 / 24);
  assert.equal(segment.kind, "closed");
});

test("a stay elsewhere in the year draws nothing in an unrelated week", () => {
  const p = house([["2026-05-30", "2026-06-01", "b"]]);
  assert.deepEqual(L.weekSegments(p.stays, L.monthWeeks(2026, 8)[1], IN, OUT), []);
});

test("a position is split into whole tiles and a share of one", () => {
  assert.deepEqual(L.cellPosition(1.625, false), { cell: 1, part: 0.625 });
  assert.deepEqual(L.cellPosition(0, false), { cell: 0, part: 0 });
  assert.deepEqual(L.cellPosition(3, false), { cell: 3, part: 0 }); // a left edge on a boundary opens the next tile
  assert.deepEqual(L.cellPosition(3, true), { cell: 2, part: 1 });  // a right edge on it closes the previous one
  assert.deepEqual(L.cellPosition(7, true), { cell: 6, part: 1 });  // the end of the week row
  const morning = L.cellPosition(4 + 11 / 24, true);
  assert.equal(morning.cell, 4);
  near(morning.part, 11 / 24);
});

test("each house gets a short mark that no other house shares", () => {
  assert.deepEqual(L.badges(["Santa", "Vassena"]), ["S", "V"]);
  assert.deepEqual(L.badges(["Casa Mare", "Casa Lago"]), ["CM", "CL"]); // same first letter: word initials
  assert.deepEqual(L.badges(["Mare", "Monti"]), ["Ma", "Mo"]);          // one word each: two letters
  assert.deepEqual(L.badges(["Casa", "Casa"]), ["1", "2"]);             // nothing tells them apart: numbers
  assert.deepEqual(L.badges(["  lago "]), ["L"]);
  assert.deepEqual(L.badges([]), []);
});

/* ---- the family's own entries ---- */

function entry(overrides = {}) {
  return { id: "e_k3j9x2m1q8", prop: "casa", start: "2026-08-10", end: "2026-08-14", who: "Marco",
    note: "", by: "Marco", at: "2026-06-01T10:00:00Z", ...overrides };
}
const setEvent = (overrides) => ({ t: "set", ...entry(overrides) });

test("an entry is validated field by field", () => {
  const ok = L.validEntry(entry({ who: "  Nonna\tAda ", at: "ieri" }));
  assert.deepEqual([ok.who, ok.at, L.ymd(ok.start), L.ymd(ok.end)], ["Nonna Ada", "", "2026-08-10", "2026-08-14"]);
  const bad = [
    { id: "short" }, { id: "has spaces!" }, { id: 12345678 }, { prop: "Casa" }, { prop: "../x" },
    { start: "10/08/2026" }, { start: "2026-02-30" }, { end: "2026-08-10" }, { end: "2026-08-01" },
    { who: "" }, { who: "   " }, { who: "x".repeat(81) }, { who: ["Marco"] }, { note: "x".repeat(201) }, { by: "x".repeat(41) },
    { end: "2027-08-12" },                               // longer than a year
    { start: "1999-12-30", end: "2000-01-02" }, { start: "2099-12-30", end: "2100-01-02" } // outside the calendar's century
  ];
  for (const change of bad) assert.equal(L.validEntry(entry(change)), null, JSON.stringify(change));
  for (const junk of [null, undefined, "x", 3, []]) assert.equal(L.validEntry(junk), null);
});

test("an entry goes back to its stored form unchanged", () => {
  assert.deepEqual(L.entryRecord(L.validEntry(entry())), entry());
});

test("the log replays into the current entries", () => {
  const state = L.foldEvents([
    setEvent(),
    setEvent({ id: "e_second0001", who: "Nonna" }),
    setEvent({ end: "2026-08-16", by: "Giulia" }), // the first entry, edited later
    { t: "del", id: "e_second0001" }
  ], {});
  assert.deepEqual(Object.keys(state), ["e_k3j9x2m1q8"]);
  assert.deepEqual([L.ymd(state.e_k3j9x2m1q8.end), state.e_k3j9x2m1q8.by], ["2026-08-16", "Giulia"]);
});

test("replaying events the page already shows changes nothing", () => {
  const events = [setEvent(), setEvent({ id: "e_second0001", who: "Nonna" }), setEvent({ who: "Marco e Giulia" })];
  const once = L.foldEvents(events, {});
  // A change is shown as soon as it is saved, then confirmed by reading the log again.
  const optimistic = L.foldEvents([events[2]], L.foldEvents(events.slice(0, 2), {}));
  assert.deepEqual(L.foldEvents(events, optimistic), once);
  assert.deepEqual(L.foldEvents(events, once), once);
});

test("replay leaves its input alone and skips bad events", () => {
  const known = L.foldEvents([setEvent()], {});
  const later = L.foldEvents([
    setEvent({ id: "e_broken0001", end: "2020-01-01" }), { t: "del" }, { t: "wipe" }, "nope", null,
    { t: "snap", entries: "nope" }, { t: "snap", entries: [entry(), { id: "bad" }] }, // a half-valid snapshot is ignored whole
    setEvent({ id: "e_second0001", who: "Nonna" })
  ], known);
  assert.deepEqual(Object.keys(later).sort(), ["e_k3j9x2m1q8", "e_second0001"]);
  assert.deepEqual(Object.keys(known), ["e_k3j9x2m1q8"]);
});

test("a snapshot replaces everything before it", () => {
  const state = L.foldEvents([
    setEvent(), { t: "snap", entries: [entry({ id: "e_kept000001", who: "Elvio" })] }, setEvent({ id: "e_after00001", who: "Nonna" })
  ], {});
  assert.deepEqual(Object.keys(state).sort(), ["e_after00001", "e_kept000001"]);
});

test("entries are listed by date, optionally for one house", () => {
  const state = L.foldEvents([
    setEvent({ id: "e_later00001", start: "2026-09-01", end: "2026-09-03" }),
    setEvent({ id: "e_other00001", prop: "lago", start: "2026-07-01", end: "2026-07-03" }),
    setEvent({ id: "e_sooner0001", start: "2026-06-01", end: "2026-06-03" })
  ], {});
  assert.deepEqual(L.entriesOf(state).map((e) => e.id), ["e_sooner0001", "e_other00001", "e_later00001"]);
  assert.deepEqual(L.entriesOf(state, "casa").map((e) => e.id), ["e_sooner0001", "e_later00001"]);
});

test("an entry inside dates blocked on the portal is protected", () => {
  const p = house([["2026-08-01", "2026-08-31", "c"]]);
  const status = L.entryStatus(L.validEntry(entry()), p);
  assert.deepEqual([status.state, status.open, status.guests], ["blocked", [], null]);
});

test("an entry on dates still for sale is pending, with the nights left to close", () => {
  const free = L.entryStatus(L.validEntry(entry()), house([]));
  assert.equal(free.state, "pending");
  assert.deepEqual(free.open.map((r) => [L.ymd(r.start), L.ymd(r.end)]), [["2026-08-10", "2026-08-14"]]);
  // Partly blocked: only the nights still open are listed, as separate runs.
  const partly = L.entryStatus(L.validEntry(entry({ start: "2026-08-08", end: "2026-08-16" })),
    house([["2026-08-10", "2026-08-12", "c"]]));
  assert.equal(partly.state, "pending");
  assert.deepEqual(partly.open.map((r) => [L.ymd(r.start), L.ymd(r.end)]),
    [["2026-08-08", "2026-08-10"], ["2026-08-12", "2026-08-16"]]);
});

test("an entry over a guests' booking is a conflict", () => {
  const p = house([["2026-08-12", "2026-08-15", "b", "Airbnb", 4]]);
  const status = L.entryStatus(L.validEntry(entry()), p);
  assert.equal(status.state, "conflict");
  assert.deepEqual([L.ymd(status.guests.start), L.ymd(status.guests.end)], ["2026-08-12", "2026-08-15"]);
});

test("arriving the day guests leave, or leaving the day they arrive, is no conflict", () => {
  const p = house([["2026-08-05", "2026-08-10", "b"], ["2026-08-14", "2026-08-18", "b"]]);
  assert.equal(L.entryStatus(L.validEntry(entry()), p).state, "pending"); // 10 -> 14 fits exactly between them
});

test("nights the portal says nothing about are listed with those still for sale", () => {
  const p = house([], "2026-01-01", "2026-08-12");
  const status = L.entryStatus(L.validEntry(entry()), p);
  assert.equal(status.state, "pending"); // the 10th and the 11th are known to be for sale
  assert.deepEqual(status.open.map((r) => [L.ymd(r.start), L.ymd(r.end)]), [["2026-08-10", "2026-08-14"]]);
});

test("an entry the portal publishes nothing about yet cannot be called blocked or for sale", () => {
  const p = house([["2026-08-01", "2026-08-12", "c"]], "2026-01-01", "2026-08-12");
  const beyond = L.entryStatus(L.validEntry(entry({ start: "2026-09-01", end: "2026-09-05" })), p);
  assert.equal(beyond.state, "unknown");
  assert.deepEqual(beyond.open.map((r) => [L.ymd(r.start), L.ymd(r.end)]), [["2026-09-01", "2026-09-05"]]);
  // Blocked as far as the data goes, unknown after: still not "blocked".
  const partly = L.entryStatus(L.validEntry(entry()), p);
  assert.equal(partly.state, "unknown");
  assert.deepEqual(partly.open.map((r) => [L.ymd(r.start), L.ymd(r.end)]), [["2026-08-12", "2026-08-14"]]);
});

test("nights with guests are not listed among those to close", () => {
  const p = house([["2026-08-11", "2026-08-12", "b"]]);
  const status = L.entryStatus(L.validEntry(entry()), p);
  assert.equal(status.state, "conflict");
  assert.deepEqual(status.open.map((r) => [L.ymd(r.start), L.ymd(r.end)]),
    [["2026-08-10", "2026-08-11"], ["2026-08-12", "2026-08-14"]]);
});

test("an entry of a house that could not be read has no status", () => {
  const missing = L.prepare({ id: "casa", label: "Casa", missing: true });
  assert.equal(L.entryStatus(L.validEntry(entry()), missing).state, "unknown");
  assert.equal(L.entryStatus(L.validEntry(entry()), null).state, "unknown");
});

test("saving is refused with a reason", () => {
  const p = house([["2026-08-20", "2026-08-25", "b"]]);
  const existing = L.foldEvents([setEvent()], {});
  const candidate = (overrides) => {
    const c = { id: "e_new0000001", prop: "casa", start: day("2026-09-01"), end: day("2026-09-05"), who: "Giulia", ...overrides };
    return L.checkEntry(c, [p], existing);
  };
  assert.deepEqual(candidate({}), { ok: true });
  assert.equal(candidate({ prop: "montagna" }).code, "house");
  assert.equal(candidate({ who: "  " }).code, "who");
  assert.equal(candidate({ end: day("2026-09-01") }).code, "dates");
  assert.equal(candidate({ start: NaN }).code, "dates");
  assert.equal(candidate({ start: day("1999-12-31"), end: day("2000-01-03") }).code, "dates");
  assert.equal(candidate({ start: day("2099-12-30"), end: day("2100-01-02") }).code, "dates");
  assert.equal(candidate({ start: 1e9, end: 1e9 + 2 }).code, "dates"); // a year no date field should produce
  assert.equal(candidate({ start: day("2026-09-01"), end: day("2027-09-03") }).code, "long");
  assert.deepEqual(candidate({ start: day("2026-09-01"), end: day("2026-09-01") + L.MAX_NIGHTS }), { ok: true });
  const guests = candidate({ start: day("2026-08-22"), end: day("2026-08-27") });
  assert.equal(guests.code, "guests");
  assert.equal(L.ymd(guests.detail.start), "2026-08-20");
  const overlap = candidate({ start: day("2026-08-12"), end: day("2026-08-18") });
  assert.deepEqual([overlap.code, overlap.detail.id], ["overlap", "e_k3j9x2m1q8"]);
  // Back to back with another family stay is fine, and so is editing an entry over itself.
  assert.deepEqual(candidate({ start: day("2026-08-14"), end: day("2026-08-18") }), { ok: true });
  assert.deepEqual(candidate({ id: "e_k3j9x2m1q8", start: day("2026-08-11"), end: day("2026-08-15") }), { ok: true });
});

test("whatever the form accepts, the log's readers accept too", () => {
  const p = house([]);
  const cases = [
    { start: day("2026-09-01"), end: day("2026-09-01") + L.MAX_NIGHTS },
    { start: day("2000-01-01"), end: day("2000-01-02") },
    { start: day("2099-12-31"), end: day("2100-01-01") }
  ];
  for (const dates of cases) {
    const c = { id: "e_new0000001", prop: "casa", who: "Giulia", ...dates };
    assert.deepEqual(L.checkEntry(c, [p], {}), { ok: true });
    assert.ok(L.validEntry({ ...entry(), id: c.id, start: L.ymd(c.start), end: L.ymd(c.end) }) !== null);
  }
});

test("a new period is proposed where nothing is in the way", () => {
  const p = house([
    ["2026-08-01", "2026-08-05", "b"],              // guests
    ["2026-08-05", "2026-08-12", "c"],              // a short block right after them
    ["2026-08-20", "2026-08-21", "b"],
    ["2026-10-01", "2026-12-01", "c"]               // a long block
  ]);
  const draft = (ymdText, entries = {}) => {
    const d = L.draftPeriod(p, day(ymdText), entries);
    return [d.prop, L.ymd(d.start), L.ymd(d.end)];
  };
  assert.deepEqual(draft("2026-08-14"), ["casa", "2026-08-14", "2026-08-16"]); // free: two nights
  assert.deepEqual(draft("2026-08-19"), ["casa", "2026-08-19", "2026-08-20"]); // one, guests arrive the next day
  assert.deepEqual(draft("2026-08-08"), ["casa", "2026-08-05", "2026-08-12"]); // a short block is proposed whole
  assert.deepEqual(draft("2026-10-10"), ["casa", "2026-10-10", "2026-10-17"]); // a long one a week at a time
  assert.deepEqual(draft("2026-11-28"), ["casa", "2026-11-28", "2026-12-01"]); // ...up to its end

  // The family's own periods bound the proposal on both sides.
  const taken = L.foldEvents([
    setEvent({ id: "e_before0001", start: "2026-08-05", end: "2026-08-07" }),
    setEvent({ id: "e_after00001", start: "2026-08-10", end: "2026-08-12" }),
    setEvent({ id: "e_free000001", start: "2026-08-15", end: "2026-08-18" })
  ], {});
  assert.deepEqual(draft("2026-08-08", taken), ["casa", "2026-08-07", "2026-08-10"]);
  assert.deepEqual(draft("2026-08-14", taken), ["casa", "2026-08-14", "2026-08-15"]);
  for (const from of ["2026-08-08", "2026-08-14", "2026-08-13", "2026-08-18", "2026-08-19"]) {
    const d = L.draftPeriod(p, day(from), taken);
    assert.deepEqual(L.checkEntry({ id: null, prop: "casa", who: "x", start: d.start, end: d.end }, [p], taken), { ok: true }, from);
  }
});

test("the first day worth proposing skips guests and the family's own periods", () => {
  const p = house([["2026-08-01", "2026-08-05", "b"], ["2026-08-05", "2026-08-08", "c"]], "2026-01-01", "2026-08-20");
  const first = (ymdText, entries = {}) => L.ymd(L.firstOpenDay(p, day(ymdText), entries));
  assert.equal(first("2026-07-30"), "2026-07-30");          // already free
  assert.equal(first("2026-08-02"), "2026-08-05");          // guests until the 5th; the block after them will do
  const taken = L.foldEvents([setEvent({ start: "2026-08-05", end: "2026-08-09" })], {});
  assert.equal(first("2026-08-02", taken), "2026-08-09");   // ...unless the family already has it
  // Nothing free in the known data: the day asked for comes back.
  const full = house([["2026-08-01", "2026-08-20", "b"]], "2026-01-01", "2026-08-20");
  assert.equal(L.ymd(L.firstOpenDay(full, day("2026-08-02"), {})), "2026-08-02");
  assert.equal(L.ymd(L.firstOpenDay(L.prepare({ id: "casa", label: "Casa", missing: true }), day("2026-08-02"), {})), "2026-08-02");
});

test("new entry ids are well formed", () => {
  const id = L.entryId(Uint8Array.from({ length: 12 }, (_, i) => i * 21));
  assert.equal(id.length, 14);
  assert.ok(L.validEntry(entry({ id })) !== null);
});

test("freshness flags a missed nightly run and a house left behind", () => {
  const generated = Date.UTC(2026, 7, 15, 2, 17);
  const fresh = { id: "a", missing: false, fetchedAt: generated - 60 * 1000 };
  const behind = { id: "b", missing: false, fetchedAt: generated - 30 * 3600 * 1000 };
  const gone = { id: "c", missing: true, fetchedAt: null };
  const soon = L.freshness(generated, [fresh, behind, gone], generated + 10 * 3600 * 1000);
  assert.equal(soon.stale, false);
  assert.deepEqual(soon.lagging, ["b"]);
  assert.deepEqual(soon.missing, ["c"]);
  assert.equal(L.freshness(generated, [fresh], generated + 37 * 3600 * 1000).stale, true);
});

/* ---- economics: every manager publishes their own columns ---- */

function owner(kpis, extra = {}) {
  return { kpi_columns: ["ADR", "Imposta", "Netto proprietario"], kpi_roles: { adr: 0, net: 2 }, kpis, ...extra };
}

test("year rows rebuild gross as nights x ADR and occupancy on calendar days", () => {
  const rows = L.yearRows(owner([
    { y: 2026, m: 6, nights: 14, occ_pct: 47, vals: [25964, 76335, 184103] },
    { y: 2027, m: 6, nights: 3, occ_pct: 10, vals: [10000, 0, 0] }
  ]), 2026);
  assert.equal(rows.length, 12);
  const june = rows[5];
  assert.equal(june.gross_cents, 14 * 25964);
  assert.equal(june.net_cents, 184103);
  assert.deepEqual(june.vals, [25964, 76335, 184103]);
  assert.ok(Math.abs(june.occupancy - 14 / 30) < 1e-12);
  assert.equal(rows[0].nights, 0);
  assert.equal(rows[0].gross_cents, null);
  assert.deepEqual(rows[0].vals, [null, null, null]);
});

test("a month the portal lists with empty amounts stays empty, not zero", () => {
  const rows = L.yearRows(owner([{ y: 2026, m: 3, nights: 0, occ_pct: 0, vals: [null, null, null] }]), 2026);
  assert.deepEqual([rows[2].adr_cents, rows[2].gross_cents, rows[2].net_cents], [null, null, null]);
});

test("a house's year uses the portal's own totals when it lists them", () => {
  const months = [
    { y: 2026, m: 5, nights: 15, vals: [10000, 33333, 100001] },
    { y: 2026, m: 6, nights: 11, vals: [20000, 33333, 100001] }
  ];
  const summed = L.houseYear(owner(months), 2026);
  assert.deepEqual([summed.nights, summed.net_cents], [26, 200002]);
  assert.deepEqual(summed.vals, [Math.round((15 * 10000 + 11 * 20000) / 26), 66666, 200002]);
  assert.equal(summed.gross_cents, 15 * 10000 + 11 * 20000);
  // The portal rounds its total on its own: the page must show the portal's figure.
  const listed = L.houseYear(owner(months, { kpi_totals: [{ y: 2026, nights: 26, occ_pct: 7, vals: [14231, 66667, 200001] }] }), 2026);
  assert.deepEqual(listed.vals, [14231, 66667, 200001]);
  assert.equal(listed.net_cents, 200001);
  assert.ok(Math.abs(listed.net_ratio - 200001 / 370000) < 1e-12);
  assert.ok(Math.abs(listed.occupancy - 26 / 365) < 1e-12);
  // A total the portal leaves blank falls back to the months.
  const partial = L.houseYear(owner(months, { kpi_totals: [{ y: 2026, nights: 26, vals: [null, null, 200001] }] }), 2026);
  assert.deepEqual(partial.vals, [summed.vals[0], 66666, 200001]);
  // Totals of another year are not used.
  assert.equal(L.houseYear(owner(months, { kpi_totals: [{ y: 2027, nights: 1, vals: [1, 1, 1] }] }), 2026).net_cents, 200002);
});

test("houses whose managers publish different columns still add up", () => {
  const first = L.houseYear(owner([{ y: 2026, m: 5, nights: 2, vals: [25000, 10500, 27300] }]), 2026);
  const second = L.houseYear({
    kpi_columns: ["ADR", "PM", "Proprietario", "Imposta", "Addebiti al proprietario", "Netto proprietario"],
    kpi_roles: { adr: 0, net: 5 },
    kpis: [{ y: 2026, m: 5, nights: 15, vals: [68877, 199558, 618302, 129844, 0, 488459] }]
  }, 2026);
  const total = L.combine([first, second], 2026);
  assert.equal(total.nights, 17);
  assert.equal(total.net_cents, 27300 + 488459);
  assert.equal(total.gross_cents, 2 * 25000 + 15 * 68877);
  assert.equal(total.adr_cents, Math.round((2 * 25000 + 15 * 68877) / 17)); // weighted by nights
  assert.ok(Math.abs(total.occupancy - 17 / 730) < 1e-12); // two houses, 365 days each
  assert.ok(Math.abs(total.net_ratio - (27300 + 488459) / (2 * 25000 + 15 * 68877)) < 1e-12);
});

test("a house without recognisable ADR or net column yields no gross and no net", () => {
  const year = L.houseYear({ kpi_columns: ["Incasso"], kpi_roles: {}, kpis: [{ y: 2026, m: 3, nights: 2, vals: [1000] }] }, 2026);
  assert.deepEqual([year.rows[2].gross_cents, year.rows[2].net_cents, year.rows[2].adr_cents], [null, null, null]);
  assert.deepEqual([year.nights, year.gross_cents, year.net_cents, year.net_ratio], [2, null, null, null]);
  assert.deepEqual(year.vals, [1000]);
  // Next to a house that does have them, it adds its nights and nothing else.
  const other = L.houseYear(owner([{ y: 2026, m: 3, nights: 4, vals: [10000, 100, 20000] }]), 2026);
  const total = L.combine([year, other], 2026);
  assert.deepEqual([total.nights, total.gross_cents, total.net_cents], [6, 40000, 20000]);
});

test("a year without sales has no ADR and no ratio", () => {
  const total = L.combine([L.houseYear(owner([]), 2027)], 2027);
  assert.deepEqual([total.nights, total.adr_cents, total.gross_cents, total.net_cents, total.net_ratio], [0, null, null, null, null]);
  assert.deepEqual(L.combine([], 2027).occupancy, null);
});

test("column totals add amounts and weight the daily rate by nights", () => {
  const rows = L.yearRows(owner([
    { y: 2026, m: 5, nights: 2, vals: [10000, 4200, 11000] },
    { y: 2026, m: 6, nights: 1, vals: [40000, 8400, null] }
  ]), 2026);
  assert.deepEqual(L.columnTotals(rows, 3, 0), [20000, 12600, 11000]); // ADR: (2x100 + 1x400) / 3 nights
  assert.deepEqual(L.columnTotals(L.yearRows(owner([]), 2026), 3, 0), [null, null, null]);
  assert.deepEqual(L.columnTotals(rows, 3, null), [50000, 12600, 11000]); // no rate role: plain sums
});

test("reservations are listed by check-in year with their column totals", () => {
  const house = owner([], {
    res_columns: ["Totale pernottamento", "Commissioni OTA", "Netto proprietario"],
    res_roles: { gross: 0, net: 2 },
    reservations: [
      ["2026-05-30", "2026-06-02", "Airbnb", 4, [75000, 11250, 36000]],
      ["2026-12-31", "2027-01-03", "Diretta", null, [51300, null, 18960]],
      ["2027-02-10", "2027-02-12", "Booking", 2, [30000, 4500, 14000]]
    ]
  });
  const year = L.yearReservations(house, 2026);
  assert.equal(year.rows.length, 2); // the New Year booking belongs to its check-in year
  assert.equal(year.nights, 6);
  assert.deepEqual(year.totals, [126300, 11250, 54960]);
  assert.deepEqual([year.rows[0].channel, year.rows[0].guests, year.rows[0].nights], ["Airbnb", 4, 3]);
  assert.equal(year.rows[1].guests, null);
  assert.deepEqual(L.yearReservations(house, 2025), { rows: [], totals: [null, null, null], nights: 0 });
  assert.deepEqual(L.yearReservations(owner([]), 2026), { rows: [], totals: [], nights: 0 });
});

test("the years on offer cover statistics and reservations alike", () => {
  const house = owner([{ y: 2026, m: 1, nights: 0, vals: [null, null, null] }], {
    reservations: [["2027-02-10", "2027-02-12", "Booking", 2, []]]
  });
  assert.deepEqual(L.kpiYears([house, owner([{ y: 2025, m: 3, nights: 1, vals: [1, 1, 1] }])]), [2025, 2026, 2027]);
  assert.deepEqual(L.kpiYears([]), []);
});

test("a stay keeps the channel and guest count the portal lists", () => {
  const p = house([["2026-08-01", "2026-08-07", "b", "Airbnb", 4], ["2026-08-10", "2026-08-12", "b"], ["2026-09-01", "2026-09-03", "c"]]);
  assert.deepEqual(p.stays.map((s) => [s.kind, s.channel, s.guests]),
    [["booking", "Airbnb", 4], ["booking", null, null], ["closed", null, null]]);
});

test("axis scale picks round euro ticks that cover the maximum", () => {
  assert.deepEqual(L.axisScale(364123), { max: 400000, ticks: [0, 100000, 200000, 300000, 400000] });
  const small = L.axisScale(27300);
  assert.equal(small.ticks[0], 0);
  assert.ok(small.max >= 27300);
  assert.ok(small.ticks.length <= 6);
});
