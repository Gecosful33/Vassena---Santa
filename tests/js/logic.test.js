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
