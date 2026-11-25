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

test("a house that could not be read is unknown and lists no guests", () => {
  const p = L.prepare({ id: "x", label: "X", missing: true });
  assert.equal(L.dayInfo(p, day("2026-08-01")).state, "unknown");
  assert.deepEqual(L.upcomingGuests(p, day("2026-08-01")), []);
});

test("a house's photo is shown only if it is what the nightly job makes", () => {
  const data = "UklGRhoAAABXRUJQVlA4IA4AAAAwAQCdASoBAAEAAQAcJaQAA3AA/v3AgAA=";
  assert.deepEqual(L.photoUrls({ mare: { type: "image/webp", data }, lago: { type: "image/webp", data: "QUJD" } }),
    { mare: "data:image/webp;base64," + data, lago: "data:image/webp;base64,QUJD" });
  // Anything else is left out: another format (one that can carry a script, most of all),
  // text that is not base64, a value of the wrong kind.
  assert.deepEqual(L.photoUrls({
    svg: { type: "image/svg+xml", data },
    html: { type: "text/html", data },
    jpeg: { type: "image/jpeg", data },
    quote: { type: "image/webp", data: 'AAAA" onerror="alert(1)' },
    spaces: { type: "image/webp", data: "AAAA AAAA" },
    empty: { type: "image/webp", data: "" },
    number: { type: "image/webp", data: 7 },
    missing: { type: "image/webp" },
    none: null,
    text: "image/webp"
  }), {});
  for (const odd of [undefined, null, "foto", 7, []]) assert.deepEqual(L.photoUrls(odd), {});
});

test("the guests to come are the stays with guests that have not ended, soonest first", () => {
  const p = house([
    ["2026-07-20", "2026-07-25", "b"],                 // gone
    ["2026-08-01", "2026-08-07", "b", "Airbnb", 4],
    ["2026-08-07", "2026-08-10", "b", "Diretta", 2],   // same-day turnover
    ["2026-08-20", "2026-08-25", "c"],                 // blocked dates are not guests
    ["2026-12-30", "2027-01-02", "b"]
  ]);
  const list = (ymdText) => L.upcomingGuests(p, day(ymdText)).map((g) => [L.ymd(g.stay.start), g.current, g.inDays]);
  assert.deepEqual(list("2026-07-30"), [["2026-08-01", false, 2], ["2026-08-07", false, 8], ["2026-12-30", false, 153]]);
  assert.deepEqual(list("2026-08-01"), [["2026-08-01", true, 0], ["2026-08-07", false, 6], ["2026-12-30", false, 151]]);
  // On a turnover day the guests leaving that morning are no longer listed; those arriving are in the house tonight.
  assert.deepEqual(list("2026-08-07"), [["2026-08-07", true, 0], ["2026-12-30", false, 145]]);
  assert.deepEqual(list("2026-08-10"), [["2026-12-30", false, 142]]);
  assert.deepEqual(list("2027-01-02"), []);
  const first = L.upcomingGuests(p, day("2026-07-30"))[0].stay;
  assert.deepEqual([first.channel, first.guests], ["Airbnb", 4]); // what the portal says about them comes along
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
    setEvent(), { t: "snap", entries: [entry({ id: "e_kept000001", who: "Paolo" })] }, setEvent({ id: "e_after00001", who: "Nonna" })
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
  assert.deepEqual(candidate({}), { ok: true, shared: [] });
  assert.equal(candidate({ prop: "montagna" }).code, "house");
  assert.equal(candidate({ who: "  " }).code, "who");
  assert.equal(candidate({ end: day("2026-09-01") }).code, "dates");
  assert.equal(candidate({ start: NaN }).code, "dates");
  assert.equal(candidate({ start: day("1999-12-31"), end: day("2000-01-03") }).code, "dates");
  assert.equal(candidate({ start: day("2099-12-30"), end: day("2100-01-02") }).code, "dates");
  assert.equal(candidate({ start: 1e9, end: 1e9 + 2 }).code, "dates"); // a year no date field should produce
  assert.equal(candidate({ start: day("2026-09-01"), end: day("2027-09-03") }).code, "long");
  assert.equal(candidate({ start: day("2026-09-01"), end: day("2026-09-01") + L.MAX_NIGHTS }).ok, true);
  const guests = candidate({ start: day("2026-08-22"), end: day("2026-08-27") });
  assert.equal(guests.code, "guests");
  assert.equal(L.ymd(guests.detail.start), "2026-08-20");
});

test("two family periods may share nights, and the check says with whom", () => {
  const p = house([["2026-08-20", "2026-08-25", "b"]]);
  const existing = L.foldEvents([
    setEvent({ who: "Famiglia Rossi" }),                                                       // 10 -> 14 August
    setEvent({ id: "e_second0001", who: "Famiglia Bianchi", start: "2026-08-13", end: "2026-08-16" })
  ], {});
  const shared = (overrides) => {
    const c = { id: "e_new0000001", prop: "casa", start: day("2026-08-12"), end: day("2026-08-15"), who: "Zii", ...overrides };
    const check = L.checkEntry(c, [p], existing);
    assert.equal(check.ok, true);
    return check.shared.map((other) => other.who);
  };
  assert.deepEqual(shared({}), ["Famiglia Rossi", "Famiglia Bianchi"]);       // listed by date
  assert.deepEqual(shared({ start: day("2026-08-15"), end: day("2026-08-18") }), ["Famiglia Bianchi"]);
  // Arriving the day another family leaves is not sharing, and a period is never listed against itself.
  assert.deepEqual(shared({ start: day("2026-08-16"), end: day("2026-08-18") }), []);
  assert.deepEqual(shared({ id: "e_k3j9x2m1q8", start: day("2026-08-10"), end: day("2026-08-13") }), []);
  // Another house is another matter.
  assert.deepEqual(L.checkEntry({ id: null, prop: "lago", start: day("2026-08-12"), end: day("2026-08-15"), who: "Zii" },
    [p, { ...p, id: "lago" }], existing).shared, []);
  // Guests are still a refusal, shared or not.
  assert.equal(L.checkEntry({ id: null, prop: "casa", start: day("2026-08-13"), end: day("2026-08-22"), who: "Zii" }, [p], existing).code, "guests");
});

function names(run) { return run.entries.map((entry) => entry.who).join(" + "); }
const at = (ymdText, hour) => day(ymdText) + hour / 24;

test("periods that share nights are drawn as stretches, one bar for the nights in common", () => {
  const p = house([["2026-08-01", "2026-08-31", "c"]]); // all blocked: hours 15:00 and 11:00
  const runs = L.familyRuns(L.entriesOf(L.foldEvents([
    setEvent({ who: "Rossi", start: "2026-08-10", end: "2026-08-20" }),
    setEvent({ id: "e_second0001", who: "Bianchi", start: "2026-08-14", end: "2026-08-17" })
  ], {}), "casa"), p);
  assert.deepEqual(runs.map((run) => [names(run), run.from, run.to, run.joinLeft, run.joinRight]), [
    ["Rossi", at("2026-08-10", 15), at("2026-08-14", 15), false, true],          // alone until the others check in...
    ["Rossi + Bianchi", at("2026-08-14", 15), at("2026-08-17", 11), true, true], // ...together until they check out...
    ["Rossi", at("2026-08-17", 11), at("2026-08-20", 11), true, false]           // ...alone again, with no gap anywhere
  ]);
});

test("periods that do not share nights stay apart, turnover gap included", () => {
  const p = house([]);
  const runs = L.familyRuns(L.entriesOf(L.foldEvents([
    setEvent({ who: "Rossi", start: "2026-08-10", end: "2026-08-14" }),
    setEvent({ id: "e_second0001", who: "Bianchi", start: "2026-08-14", end: "2026-08-16" }), // same-day turnover
    setEvent({ id: "e_third00001", who: "Zii", start: "2026-08-20", end: "2026-08-22" })
  ], {}), "casa"), p);
  assert.deepEqual(runs.map((run) => [names(run), run.from, run.to, run.joinLeft, run.joinRight]), [
    ["Rossi", at("2026-08-10", 15), at("2026-08-14", 11), false, false],
    ["Bianchi", at("2026-08-14", 15), at("2026-08-16", 11), false, false],
    ["Zii", at("2026-08-20", 15), at("2026-08-22", 11), false, false]
  ]);
  assert.deepEqual(L.familyRuns([], p), []);
});

test("two periods on the very same dates are one stretch with both", () => {
  const p = house([]);
  const runs = L.familyRuns(L.entriesOf(L.foldEvents([
    setEvent({ who: "Rossi" }), setEvent({ id: "e_second0001", who: "Bianchi" })
  ], {}), "casa"), p);
  assert.deepEqual(runs.map((run) => [names(run), run.joinLeft, run.joinRight]), [["Rossi + Bianchi", false, false]]);
});

test("a shared stretch takes the most urgent standing among its periods", () => {
  // Blocked from the 10th to the 14th only; guests from the 18th.
  const p = house([["2026-08-10", "2026-08-14", "c"], ["2026-08-18", "2026-08-20", "b"]]);
  const runs = L.familyRuns(L.entriesOf(L.foldEvents([
    setEvent({ who: "Rossi", start: "2026-08-10", end: "2026-08-14" }),                          // blocked
    setEvent({ id: "e_second0001", who: "Bianchi", start: "2026-08-12", end: "2026-08-16" }),    // partly for sale
    setEvent({ id: "e_third00001", who: "Zii", start: "2026-08-15", end: "2026-08-19" })         // guests on its last night
  ], {}), "casa"), p);
  assert.deepEqual(runs.map((run) => [names(run), run.state]), [
    ["Rossi", "blocked"], ["Rossi + Bianchi", "pending"], ["Bianchi", "pending"],
    ["Bianchi + Zii", "conflict"], ["Zii", "conflict"]
  ]);
});

test("stretches are cut to a week row like stays are", () => {
  const p = house([]);
  const runs = L.familyRuns(L.entriesOf(L.foldEvents([
    setEvent({ who: "Rossi", start: "2026-08-14", end: "2026-08-18" })   // Friday to Tuesday
  ], {}), "casa"), p);
  const [first, second] = L.monthWeeks(2026, 8).slice(2, 4);            // the weeks of the 10th and of the 17th
  const [before] = L.clipToWeek(runs, first), [after] = L.clipToWeek(runs, second);
  near(before.left, 4 + 15 / 24);  // Friday, from the check-in hour...
  near(before.right, 7);           // ...to the end of the row
  assert.deepEqual([before.cutLeft, before.cutRight, names(before.span)], [false, true, "Rossi"]);
  near(after.left, 0);
  near(after.right, 1 + 11 / 24);  // Tuesday, until the check-out hour
  assert.deepEqual([after.cutLeft, after.cutRight], [true, false]);
  assert.deepEqual(L.clipToWeek(runs, L.monthWeeks(2026, 8)[0]), []);
});

test("a name is shortened by dropping the word for family", () => {
  assert.equal(L.shortName("Famiglia Rossi"), "Rossi");
  assert.equal(L.shortName("famiglia rossi"), "rossi");
  assert.equal(L.shortName("Fam. Bianchi"), "Bianchi");
  assert.equal(L.shortName("Zii di Milano"), "Zii di Milano");
  assert.equal(L.shortName("Famiglia"), "Famiglia"); // nothing would be left
});

const STATUS_CASES = require("../fixtures/entry_status.json");

test("the page judges a period as the nightly job does", () => {
  const calendar = L.prepare({ id: "casa", label: "Casa", unit: null, fetched_at: "2026-06-01T02:00:00Z", ...STATUS_CASES.house });
  for (const item of STATUS_CASES.cases) {
    const period = L.validEntry(entry({ start: item.start, end: item.end }));
    assert.equal(L.entryStatus(period, calendar).state, item.state, item.why);
  }
  assert.ok(STATUS_CASES.cases.length >= 10);
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
    assert.deepEqual(L.checkEntry(c, [p], {}), { ok: true, shared: [] });
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
    // A proposal is always one that can be saved, and by default it does not land on another family's nights.
    assert.deepEqual(L.checkEntry({ id: null, prop: "casa", who: "x", start: d.start, end: d.end }, [p], taken), { ok: true, shared: [] }, from);
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
    { y: 2026, m: 6, nights: 12, occ_pct: 40, vals: [24150, 60858, 171342] },
    { y: 2027, m: 6, nights: 3, occ_pct: 10, vals: [10000, 0, 0] }
  ]), 2026);
  assert.equal(rows.length, 12);
  const june = rows[5];
  assert.equal(june.gross_cents, 12 * 24150);
  assert.equal(june.net_cents, 171342);
  assert.deepEqual(june.vals, [24150, 60858, 171342]);
  assert.ok(Math.abs(june.occupancy - 12 / 30) < 1e-12);
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
  const first = L.houseYear(owner([{ y: 2026, m: 5, nights: 2, vals: [24000, 10080, 26240] }]), 2026);
  const second = L.houseYear({
    kpi_columns: ["ADR", "PM", "Proprietario", "Imposta", "Addebiti al proprietario", "Netto proprietario"],
    kpi_roles: { adr: 0, net: 5 },
    kpis: [{ y: 2026, m: 5, nights: 12, vals: [61250, 183750, 551250, 115763, 0, 435487] }]
  }, 2026);
  const total = L.combine([first, second], 2026);
  assert.equal(total.nights, 14);
  assert.equal(total.net_cents, 26240 + 435487);
  assert.equal(total.gross_cents, 2 * 24000 + 12 * 61250);
  assert.equal(total.adr_cents, Math.round((2 * 24000 + 12 * 61250) / 14)); // weighted by nights
  assert.ok(Math.abs(total.occupancy - 14 / 730) < 1e-12); // two houses, 365 days each
  assert.ok(Math.abs(total.net_ratio - (26240 + 435487) / (2 * 24000 + 12 * 61250)) < 1e-12);
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

test("the years on offer are those of the monthly statistics, of any house", () => {
  const house = owner([{ y: 2026, m: 1, nights: 0, vals: [null, null, null] }, { y: 2027, m: 2, nights: 2, vals: [1, 1, 1] }]);
  assert.deepEqual(L.kpiYears([house, owner([{ y: 2025, m: 3, nights: 1, vals: [1, 1, 1] }])]), [2025, 2026, 2027]);
  assert.deepEqual(L.kpiYears([owner([])]), []);
  assert.deepEqual(L.kpiYears([]), []);
});

test("a stay keeps the channel and guest count the portal lists", () => {
  const p = house([["2026-08-01", "2026-08-07", "b", "Airbnb", 4], ["2026-08-10", "2026-08-12", "b"], ["2026-09-01", "2026-09-03", "c"]]);
  assert.deepEqual(p.stays.map((s) => [s.kind, s.channel, s.guests]),
    [["booking", "Airbnb", 4], ["booking", null, null], ["closed", null, null]]);
});

test("axis scale picks round euro ticks that cover the maximum", () => {
  assert.deepEqual(L.axisScale(357500), { max: 400000, ticks: [0, 100000, 200000, 300000, 400000] });
  const small = L.axisScale(31500);
  assert.equal(small.ticks[0], 0);
  assert.ok(small.max >= 31500);
  assert.ok(small.ticks.length <= 6);
});

/* ---- profit and loss: the portals' income against the owner's sheet of running costs ---- */

const COSTS = {
  year: 2026, updated: "2026-09-01",
  lines: [
    { label: "IMU", amounts: { mare: 120000, lago: 300000 }, note: "stima" },
    { label: "Spese condominiali", amounts: { mare: 90000, lago: null }, note: "" },
    { label: "Giardiniere", amounts: { mare: null, lago: 150050 } }
  ]
};
const MARE = { id: "mare", ...owner([
  { y: 2026, m: 7, nights: 10, vals: [30000, 0, 180000] },
  { y: 2026, m: 8, nights: 20, vals: [36000, 0, 432000] }
]) };
const LAGO = { id: "lago", ...owner([{ y: 2026, m: 8, nights: 15, vals: [40000, 0, 400000] }]) };

test("the year's result sets each house's net income against its costs", () => {
  const result = L.profitAndLoss([MARE, LAGO], COSTS, 2026);
  const [mare, lago] = result.houses;
  assert.deepEqual(
    [mare.nights, mare.adr_cents, mare.gross_cents, mare.withheld_cents, mare.net_cents, mare.costs_cents, mare.margin_cents],
    [30, 34000, 1020000, 408000, 612000, 210000, 402000]);
  assert.ok(Math.abs(mare.withheld_ratio - 0.4) < 1e-12);            // of the gross
  assert.ok(Math.abs(mare.margin_ratio - 402000 / 612000) < 1e-12);  // of the net
  assert.equal(mare.breakeven_nights, 11);                           // 2,100 at 204 a night: 10.3 nights
  // The second house costs more than it earns: the loss is shown as one.
  assert.deepEqual([lago.net_cents, lago.costs_cents, lago.margin_cents, lago.breakeven_nights], [400000, 450050, -50050, 17]);
  assert.ok(lago.margin_ratio < 0);
  // All together: amounts add up, the daily rate is weighted by nights.
  const total = result.total;
  assert.deepEqual(
    [total.nights, total.adr_cents, total.gross_cents, total.withheld_cents, total.net_cents, total.costs_cents, total.margin_cents],
    [45, 36000, 1620000, 608000, 1012000, 660050, 351950]);
  assert.equal(total.breakeven_nights, 30);
  assert.equal(total.margin_cents, mare.margin_cents + lago.margin_cents);
});

test("the costs are listed house by house, with a blank where one does not apply", () => {
  const result = L.profitAndLoss([MARE, LAGO], COSTS, 2026);
  assert.deepEqual(result.lines, [
    { label: "IMU", note: "stima", amounts: [120000, 300000], total: 420000 },
    { label: "Spese condominiali", note: "", amounts: [90000, null], total: 90000 },
    { label: "Giardiniere", note: "", amounts: [null, 150050], total: 150050 }
  ]);
  // In the order of the houses on the page, whatever the order in the sheet.
  assert.deepEqual(L.profitAndLoss([LAGO, MARE], COSTS, 2026).lines[0].amounts, [300000, 120000]);
  // One house on its own: the other's costs are not its own, and a cost that is only the other's goes.
  const alone = L.profitAndLoss([MARE], COSTS, 2026);
  assert.deepEqual(alone.lines.map((line) => [line.label, line.total]), [["IMU", 120000], ["Spese condominiali", 90000]]);
  assert.equal(alone.total.costs_cents, 210000);
  assert.equal(alone.total.margin_cents, alone.houses[0].margin_cents);
});

test("the nights that pay the costs are counted whole", () => {
  // 600 of net over 3 nights is 200 a night: 2,000 of costs take exactly 10, 2,000.01 take 11.
  const house = { id: "mare", ...owner([{ y: 2026, m: 8, nights: 3, vals: [30000, 0, 60000] }]) };
  const costing = (cents) => L.profitAndLoss([house], { year: 2026, lines: [{ label: "Voce", amounts: { mare: cents } }] }, 2026);
  assert.equal(costing(200000).total.breakeven_nights, 10);
  assert.equal(costing(200001).total.breakeven_nights, 11);
  assert.equal(costing(0).total.breakeven_nights, 0);
});

test("a house that sold nothing in the year shows its costs as a loss", () => {
  const idle = { id: "lago", ...owner([]) };
  const result = L.profitAndLoss([MARE, idle], COSTS, 2026);
  const lago = result.houses[1];
  assert.deepEqual([lago.nights, lago.gross_cents, lago.net_cents, lago.withheld_cents, lago.margin_cents], [0, 0, 0, 0, -450050]);
  // Nothing to divide by: no rate, no share, no count of nights.
  assert.deepEqual([lago.adr_cents, lago.withheld_ratio, lago.margin_ratio, lago.breakeven_nights], [null, null, null, null]);
  assert.equal(result.total.margin_cents, 612000 - 660050);
  // Another year of the same houses: no income at all, the costs remain.
  const next = L.profitAndLoss([MARE, idle], COSTS, 2027);
  assert.deepEqual([next.total.nights, next.total.net_cents, next.total.margin_cents, next.total.breakeven_nights], [0, 0, -660050, null]);
});

test("a house whose portal publishes no net income has no result, only costs", () => {
  const opaque = { id: "lago", kpi_columns: ["Incasso"], kpi_roles: {}, kpis: [] };
  const result = L.profitAndLoss([opaque], COSTS, 2026);
  const lago = result.houses[0];
  assert.deepEqual([lago.gross_cents, lago.net_cents, lago.withheld_cents, lago.margin_cents, lago.margin_ratio], [null, null, null, null, null]);
  assert.equal(lago.costs_cents, 450050);
});

test("a cost sheet with odd content is read for what it is worth", () => {
  const odd = { year: 2026, lines: [
    { label: "Buona", amounts: { mare: 100 } },
    { label: "Senza importi" },
    { label: 7, amounts: { mare: "12", lago: NaN } },
    { label: "Di un'altra casa", amounts: { altrove: 500 } }
  ] };
  assert.deepEqual(L.profitAndLoss([MARE], odd, 2026).lines, [{ label: "Buona", note: "", amounts: [100], total: 100 }]);
  assert.deepEqual(L.profitAndLoss([MARE], null, 2026).lines, []);
  assert.equal(L.profitAndLoss([MARE], undefined, 2026).total.costs_cents, 0);
});

test("the cost sheets on offer are the usable ones, oldest year first", () => {
  const sheets = L.costSheets([
    { year: 2027, lines: [{ label: "IMU", amounts: {} }] },
    { year: 2026, lines: [] },                 // nothing in it
    null,
    { year: "2025", lines: [{ label: "IMU" }] }, // not a year
    { year: 2025, lines: [{ label: "IMU", amounts: {} }] }
  ]);
  assert.deepEqual(sheets.map((sheet) => sheet.year), [2025, 2027]);
  assert.deepEqual(L.costSheets(undefined), []);
  assert.deepEqual(L.costSheets({ year: 2026 }), []);
});

test("a year's result needs every house read for the whole of that year", () => {
  const both = [house([], "2026-01-01", "2028-01-01"), house([], "2026-01-01", "2028-01-01")];
  assert.deepEqual(L.coveredYears(both), [2026, 2027]);
  // A house left behind by its portal narrows the years to those they share.
  assert.deepEqual(L.coveredYears([house([], "2026-01-01", "2028-01-01"), house([], "2027-01-01", "2029-01-01")]), [2027]);
  assert.deepEqual(L.coveredYears([house([], "2026-01-01", "2027-01-01"), house([], "2027-01-01", "2028-01-01")]), []);
  // Part of a year is not the year.
  assert.deepEqual(L.coveredYears([house([], "2026-03-01", "2027-12-31")]), []);
  assert.deepEqual(L.coveredYears([house([], "2026-03-01", "2028-01-01")]), [2027]);
  // A house that could not be read at all does not count; with none, no year does.
  const missing = L.prepare({ id: "x", label: "X", missing: true });
  assert.deepEqual(L.coveredYears([missing, house([], "2026-01-01", "2027-01-01")]), [2026]);
  assert.deepEqual(L.coveredYears([missing]), []);
  assert.deepEqual(L.coveredYears([]), []);
});

test("a year without a sheet of its own borrows the costs of the latest one before it", () => {
  const sheets = L.costSheets([
    { year: 2028, lines: [{ label: "IMU", amounts: { casa: 120000 } }] },
    { year: 2026, lines: [{ label: "IMU", amounts: { casa: 100000 } }] },
  ]);
  const used = (year) => L.sheetFor(sheets, year).year;
  assert.deepEqual([2026, 2027, 2028, 2029, 2035].map(used), [2026, 2026, 2028, 2028, 2028]);
  assert.equal(used(2025), 2026); // before them all: the first there is
  assert.equal(L.sheetFor([], 2026), null);
});

test("the control panel shows only what has the shape the job gives it", () => {
  const now = Date.parse("2026-11-25T11:00:00Z");
  const status = {
    checked_at: "2026-11-25T10:43:00Z",
    houses: {
      vassena: {
        wifi: { label: "Eolo", state: "online" },
        climate: [
          { label: "Piano sopra", app: "Daikin Onecta", state: "on", mode: "heat", units: 3, running: 1, room: 19.5, target: 21 },
          { label: "Piano sotto", state: "<b>", mode: "plasma" },
          { state: "on" },
        ],
        lake: {
          name: "Lago di Como", attention_cm: 100, alarm_cm: 120, level_cm: 104.5, read_at: "2026-11-25T10:00:00Z",
          stage: "normal", trend: "rising", source: "laghi.net",
          history: [["2026-11-25T04:00:00Z", 99.1], ["2026-11-25T09:00:00Z", 103], ["2026-11-25T10:00:00Z", 104.5]],
        },
      },
      santa: {},
    },
  };
  const panel = L.panelOf(status, "vassena", now);
  assert.equal(panel.stale, false);
  assert.deepEqual(panel.wifi, { label: "Eolo", state: "online" });
  assert.equal(panel.climate.length, 2); // a row without a name is not shown
  assert.deepEqual([panel.climate[1].state, panel.climate[1].mode], ["unknown", null]);
  // The level says attention whatever the stage beside it says.
  assert.equal(panel.lake.stage, "attention");
  assert.ok(Math.abs(panel.lake.change.cm - 5.4) < 1e-9 && panel.lake.change.hours === 6);
  assert.ok(Math.abs(panel.lake.toAlarm - 15.5) < 1e-9);
  assert.equal(L.panelOf(status, "santa", now), null);
  assert.equal(L.panelOf(null, "vassena", now), null);
  assert.equal(L.panelOf(status, "vassena", now + 4 * 3600000).stale, true);
  const unread = L.panelOf({ checked_at: "x", houses: { a: { lake: { attention_cm: 100, alarm_cm: 120, stage: "alarm" } } } }, "a", now);
  assert.deepEqual([unread.lake.level, unread.lake.stage, unread.stale], [null, null, true]);
  assert.equal(L.lakeChange([["2026-11-25T09:30:00Z", 1], ["2026-11-25T10:00:00Z", 2]], 6), null); // too close to tell
});
