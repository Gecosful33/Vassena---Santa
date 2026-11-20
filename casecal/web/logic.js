/* Pure calendar and KPI logic: no DOM, no clock of its own (callers pass "now").
 *
 * Days are integers (days since 1970-01-01, UTC arithmetic) so that daylight
 * saving never shifts a date. A stay covers the nights [start, end): `start`
 * is the check-in day, `end` the check-out day.
 *
 * Guests arrive from the house's check-in time and leave by its check-out time
 * (15:00 and 11:00 unless configured otherwise). On the calendar a stay therefore
 * runs from that hour of its first day to that hour of its last one.
 */
var CaseCalLogic = (function () {
  "use strict";

  var DAY_MS = 86400000;
  var STALE_AFTER_MS = 36 * 3600 * 1000; // the nightly job has clearly been missed
  var LAGGING_MS = 6 * 3600 * 1000;      // a house older than the page itself: its portal failed

  function dayOf(y, m, d) { return Math.round(Date.UTC(y, m - 1, d) / DAY_MS); }
  function dayFromYmd(text) {
    var p = String(text).split("-");
    return dayOf(+p[0], +p[1], +p[2]);
  }
  function parts(day) {
    var d = new Date(day * DAY_MS);
    return {
      y: d.getUTCFullYear(),
      m: d.getUTCMonth() + 1,
      d: d.getUTCDate(),
      dow: (d.getUTCDay() + 6) % 7 // 0 = Monday ... 6 = Sunday
    };
  }
  function ymd(day) { return new Date(day * DAY_MS).toISOString().slice(0, 10); }
  function daysInMonth(y, m) { return new Date(Date.UTC(y, m, 0)).getUTCDate(); }
  function daysInYear(y) { return dayOf(y + 1, 1, 1) - dayOf(y, 1, 1); }

  /** Today's date in the houses' time zone, whatever the viewer's own zone is. */
  function todayInZone(timeZone, nowMs) {
    var found = {};
    new Intl.DateTimeFormat("en-US", {
      timeZone: timeZone, year: "numeric", month: "numeric", day: "numeric"
    }).formatToParts(new Date(nowMs)).forEach(function (part) { found[part.type] = part.value; });
    return dayOf(+found.year, +found.month, +found.day);
  }

  /** "15:00" -> minutes after midnight; anything else falls back to the given default. */
  function clockMinutes(text, fallback) {
    var match = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(String(text || ""));
    return match ? +match[1] * 60 + +match[2] : fallback;
  }
  function clockText(minutes) {
    var h = Math.floor(minutes / 60), m = minutes % 60;
    return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m;
  }

  /** Turn one house of the family payload into the shape the views work with. */
  function prepare(raw) {
    var checkIn = clockMinutes(raw.check_in, 15 * 60), checkOut = clockMinutes(raw.check_out, 11 * 60);
    var hours = {
      checkIn: clockText(checkIn), checkOut: clockText(checkOut),
      // Where in the day cell a stay begins and ends: 15:00 is 62.5% of the way across.
      inAt: checkIn / 1440, outAt: checkOut / 1440
    };
    if (raw.missing) {
      return { id: raw.id, label: raw.label, unit: null, missing: true, stays: [],
               from: null, to: null, fetchedAt: null, hours: hours };
    }
    var stays = raw.stays.map(function (row) {
      return { start: dayFromYmd(row[0]), end: dayFromYmd(row[1]),
               kind: row[2] === "c" ? "closed" : "booking",
               channel: row.length > 3 ? row[3] : null,
               guests: row.length > 4 ? row[4] : null };
    });
    stays.sort(function (a, b) { return a.start - b.start; });
    return {
      id: raw.id, label: raw.label, unit: raw.unit || null, missing: false, stays: stays,
      from: dayFromYmd(raw.from), to: dayFromYmd(raw.to), fetchedAt: Date.parse(raw.fetched_at),
      hours: hours
    };
  }

  /**
   * A one- or two-character mark for each house, unique among them: it is written on
   * every bar so that telling the houses apart never rests on colour alone.
   * Tries the first letter, then the initials of the first two words, then the first
   * two letters; houses that still collide are numbered.
   */
  function badges(labels) {
    var names = labels.map(function (label) { return String(label || "").trim(); });
    var schemes = [
      function (name) { return name.slice(0, 1).toUpperCase(); },
      function (name) {
        var words = name.split(/\s+/).filter(Boolean);
        return words.length > 1 ? (words[0].slice(0, 1) + words[1].slice(0, 1)).toUpperCase() : "";
      },
      function (name) { return name.slice(0, 1).toUpperCase() + name.slice(1, 2).toLowerCase(); }
    ];
    for (var i = 0; i < schemes.length; i++) {
      var marks = names.map(schemes[i]);
      var distinct = {};
      marks.forEach(function (mark) { distinct[mark] = true; });
      if (marks.every(Boolean) && Object.keys(distinct).length === marks.length) return marks;
    }
    return names.map(function (_, index) { return String(index + 1); });
  }

  function covered(prop, day) { return !prop.missing && day >= prop.from && day < prop.to; }

  function stayCovering(stays, day) {
    for (var i = 0; i < stays.length; i++) {
      if (stays[i].start > day) break;
      if (day < stays[i].end) return stays[i];
    }
    return null;
  }
  function stayEnding(stays, day) {
    for (var i = 0; i < stays.length; i++) if (stays[i].end === day) return stays[i];
    return null;
  }

  /**
   * What happens on a calendar day.
   * state: status of the night that begins on `day` ("free", "booking", "closed", "unknown").
   * night: the stay occupying that night; leaving: a stay checking out that morning.
   */
  function dayInfo(prop, day) {
    if (!covered(prop, day)) return { state: "unknown", night: null, leaving: null };
    var night = stayCovering(prop.stays, day);
    return { state: night ? night.kind : "free", night: night, leaving: stayEnding(prop.stays, day) };
  }

  /**
   * Status of the night starting on `day`, with how long it lasts.
   * For a busy night `until` is the first free night after any back-to-back stays
   * (null when that lies beyond the known data). For a free night `until` is the
   * day the next stay begins; `open` means no later stay is known.
   */
  function nightStatus(prop, day) {
    if (!covered(prop, day)) return { state: "unknown", until: null, open: false };
    var stay = stayCovering(prop.stays, day);
    var i;
    if (!stay) {
      for (i = 0; i < prop.stays.length; i++) {
        if (prop.stays[i].start > day) return { state: "free", until: prop.stays[i].start, open: false };
      }
      return { state: "free", until: prop.to, open: true };
    }
    var until = stay.end;
    for (i = 0; i < prop.stays.length; i++) {
      if (prop.stays[i].start === until) until = prop.stays[i].end;
    }
    return { state: stay.kind, until: until < prop.to ? until : null, open: false };
  }

  /**
   * Maximal runs of free nights from `fromDay` to the end of the known data.
   * `open` marks the last run, which ends only because the data does.
   */
  function freeWindows(prop, fromDay) {
    var windows = [];
    if (prop.missing) return windows;
    var cursor = Math.max(fromDay, prop.from);
    for (var i = 0; i < prop.stays.length; i++) {
      var stay = prop.stays[i];
      if (stay.end <= cursor) continue;
      if (stay.start > cursor) {
        windows.push({ start: cursor, end: stay.start, nights: stay.start - cursor, open: false });
      }
      cursor = Math.max(cursor, stay.end);
    }
    if (cursor < prop.to) {
      windows.push({ start: cursor, end: prop.to, nights: prop.to - cursor, open: true });
    }
    return windows;
  }

  /** Months (as {y, m}) touched by the days in [fromDay, toDay). */
  function monthsBetween(fromDay, toDay) {
    var months = [];
    if (!(toDay > fromDay)) return months;
    var a = parts(fromDay), b = parts(toDay - 1);
    for (var y = a.y, m = a.m; y < b.y || (y === b.y && m <= b.m); m === 12 ? (y++, m = 1) : m++) {
      months.push({ y: y, m: m });
    }
    return months;
  }

  /** Monday-first weeks of a month; each week is 7 day numbers, null outside the month. */
  function monthWeeks(y, m) {
    var first = dayOf(y, m, 1), count = daysInMonth(y, m);
    var weeks = [], week = [null, null, null, null, null, null, null];
    var column = parts(first).dow;
    for (var i = 0; i < count; i++) {
      week[column] = first + i;
      if (column === 6 || i === count - 1) {
        weeks.push(week);
        week = [null, null, null, null, null, null, null];
        column = 0;
      } else {
        column++;
      }
    }
    return weeks;
  }

  /**
   * Bars to draw for one week row, in column units (0..7).
   * A stay is drawn from its check-in hour on the first day to its check-out hour on
   * the last (`inAt` and `outAt` are fractions of a day), so a same-day turnover shows
   * as two bars with the hours between check-out and check-in left open.
   * cutLeft / cutRight say the stay continues beyond this row.
   */
  function weekSegments(stays, week, inAt, outAt) {
    var c0 = -1, c1 = -1;
    for (var c = 0; c < 7; c++) {
      if (week[c] !== null) { if (c0 < 0) c0 = c; c1 = c; }
    }
    var segments = [];
    if (c0 < 0) return segments;
    var d0 = week[c0], limit = week[c1] + 1;
    for (var i = 0; i < stays.length; i++) {
      var stay = stays[i];
      var xs = Math.max(stay.start + inAt, d0);
      var xe = Math.min(stay.end + outAt, limit);
      if (xe <= xs) continue;
      segments.push({
        left: c0 + (xs - d0),
        right: c0 + (xe - d0),
        kind: stay.kind,
        cutLeft: stay.start + inAt < d0,
        cutRight: stay.end + outAt > limit,
        stay: stay
      });
    }
    return segments;
  }

  /**
   * Where a position in column units falls inside its day cell: { cell, part }.
   * Cells are separated by gaps, so a bar edge cannot be a plain percentage of the
   * row: it is "cell index" whole steps plus "part" of one cell. A right edge that
   * sits exactly on a cell boundary belongs to the end of the previous cell.
   */
  function cellPosition(position, isRightEdge) {
    var cell = Math.floor(position + 1e-9);
    var part = position - cell;
    if (part < 1e-9) part = 0;
    if (isRightEdge && part === 0 && cell > 0) { cell -= 1; part = 1; }
    return { cell: cell, part: part };
  }

  /** Is the page, or a single house, older than the nightly schedule allows? */
  function freshness(generatedAtMs, props, nowMs) {
    return {
      generatedAt: generatedAtMs,
      stale: nowMs - generatedAtMs > STALE_AFTER_MS,
      lagging: props.filter(function (p) {
        return !p.missing && generatedAtMs - p.fetchedAt > LAGGING_MS;
      }).map(function (p) { return p.id; }),
      missing: props.filter(function (p) { return p.missing; }).map(function (p) { return p.id; })
    };
  }

  /* ---- economics (owner tier) ----
   * Every manager configures their own amount columns, so a house's owner entry
   * carries the column names (kpi_columns, res_columns), the values row by row,
   * and the index of the few columns the page must recognise (kpi_roles, res_roles).
   */

  function roleIndex(roles, name) {
    return roles && typeof roles[name] === "number" ? roles[name] : null;
  }
  function known(value) { return value !== null && value !== undefined; }

  function kpiYears(ownerProps) {
    var years = {};
    ownerProps.forEach(function (p) {
      (p.kpis || []).forEach(function (k) { years[k.y] = true; });
      (p.reservations || []).forEach(function (r) { years[+r[0].slice(0, 4)] = true; });
    });
    return Object.keys(years).map(Number).sort(function (a, b) { return a - b; });
  }

  /** Twelve rows for a year of one house; months the portal did not list count as zero nights. */
  function yearRows(owner, year) {
    var columns = owner.kpi_columns || [];
    var adrAt = roleIndex(owner.kpi_roles, "adr"), netAt = roleIndex(owner.kpi_roles, "net");
    var byMonth = {};
    (owner.kpis || []).forEach(function (k) { if (k.y === year) byMonth[k.m] = k; });
    var rows = [];
    for (var m = 1; m <= 12; m++) {
      var k = byMonth[m];
      var nights = k ? k.nights : 0;
      var vals = k ? k.vals : columns.map(function () { return null; });
      var adr = adrAt === null ? null : vals[adrAt];
      var days = daysInMonth(year, m);
      rows.push({
        m: m,
        days: days,
        nights: nights,
        occupancy: nights / days,
        vals: vals,
        adr_cents: known(adr) ? adr : null,
        // Gross is not published by the statistics table: it is rebuilt as nights x ADR.
        gross_cents: known(adr) ? nights * adr : null,
        net_cents: netAt === null || !known(vals[netAt]) ? null : vals[netAt]
      });
    }
    return rows;
  }

  function sumOf(rows, field) {
    var total = 0, any = false;
    rows.forEach(function (r) {
      if (known(r[field])) { total += r[field]; any = true; }
    });
    return any ? total : null;
  }

  /**
   * Totals of each portal column over the given month rows. Amounts add up;
   * the average daily rate is weighted by nights instead.
   */
  function columnTotals(rows, columnCount, adrAt) {
    var totals = [];
    for (var i = 0; i < columnCount; i++) {
      var sum = 0, weight = 0, any = false;
      rows.forEach(function (row) {
        var value = row.vals[i];
        if (!known(value)) return;
        any = true;
        if (i === adrAt) { sum += value * row.nights; weight += row.nights; } else { sum += value; }
      });
      if (!any) totals.push(null);
      else if (i === adrAt) totals.push(weight ? Math.round(sum / weight) : null);
      else totals.push(sum);
    }
    return totals;
  }

  /**
   * One house, one year: the month rows plus the year's totals.
   * Totals are the portal's own "Tot" row where it lists one (twelve amounts
   * rounded month by month can drift from it by a cent), otherwise the months added up.
   */
  function houseYear(owner, year) {
    var rows = yearRows(owner, year);
    var adrAt = roleIndex(owner.kpi_roles, "adr"), netAt = roleIndex(owner.kpi_roles, "net");
    var vals = columnTotals(rows, (owner.kpi_columns || []).length, adrAt);
    var listed = null;
    (owner.kpi_totals || []).forEach(function (total) { if (total.y === year) listed = total; });
    if (listed) vals = vals.map(function (value, i) { return known(listed.vals[i]) ? listed.vals[i] : value; });
    var nights = listed ? listed.nights : (sumOf(rows, "nights") || 0);
    var gross = sumOf(rows, "gross_cents");
    var net = netAt === null || !known(vals[netAt]) ? null : vals[netAt];
    return {
      rows: rows, nights: nights, vals: vals, gross_cents: gross, net_cents: net,
      occupancy: nights / daysInYear(year),
      net_ratio: gross && net !== null ? net / gross : null
    };
  }

  /** Totals across the houses in scope (pass their houseYear results). */
  function combine(houses, year) {
    var nights = 0, gross = null, net = null;
    houses.forEach(function (house) {
      nights += house.nights;
      if (known(house.gross_cents)) gross = (gross || 0) + house.gross_cents;
      if (known(house.net_cents)) net = (net || 0) + house.net_cents;
    });
    return {
      nights: nights,
      occupancy: houses.length ? nights / (daysInYear(year) * houses.length) : null,
      adr_cents: gross !== null && nights > 0 ? Math.round(gross / nights) : null,
      gross_cents: gross,
      net_cents: net,
      net_ratio: gross && net !== null ? net / gross : null
    };
  }

  /** Reservations of one house whose check-in falls in `year`, with column totals. */
  function yearReservations(owner, year) {
    var columns = owner.res_columns || [];
    var rows = (owner.reservations || []).filter(function (r) {
      return +r[0].slice(0, 4) === year;
    }).map(function (r) {
      var start = dayFromYmd(r[0]), end = dayFromYmd(r[1]);
      return { start: start, end: end, nights: end - start, channel: r[2], guests: r[3], vals: r[4] };
    });
    var totals = columns.map(function (_, i) {
      var sum = 0, any = false;
      rows.forEach(function (row) { if (known(row.vals[i])) { sum += row.vals[i]; any = true; } });
      return any ? sum : null;
    });
    var nights = 0;
    rows.forEach(function (row) { nights += row.nights; });
    return { rows: rows, totals: totals, nights: nights };
  }

  /** A "nice" axis maximum and step for values in euro cents. */
  function axisScale(maxCents) {
    var euros = Math.max(1, maxCents / 100);
    var rough = euros / 5;
    var magnitude = Math.pow(10, Math.floor(Math.log(rough) / Math.LN10));
    var residual = rough / magnitude;
    var step = (residual <= 1 ? 1 : residual <= 2 ? 2 : residual <= 2.5 ? 2.5 : residual <= 5 ? 5 : 10) * magnitude;
    var ticks = [];
    for (var v = 0; v < euros + step - 1e-9; v += step) ticks.push(Math.round(v * 100));
    return { max: ticks[ticks.length - 1], ticks: ticks };
  }

  return {
    DAY_MS: DAY_MS,
    dayOf: dayOf, dayFromYmd: dayFromYmd, parts: parts, ymd: ymd,
    daysInMonth: daysInMonth, daysInYear: daysInYear, todayInZone: todayInZone,
    prepare: prepare, badges: badges, covered: covered, dayInfo: dayInfo, nightStatus: nightStatus,
    freeWindows: freeWindows, monthsBetween: monthsBetween, monthWeeks: monthWeeks,
    weekSegments: weekSegments, cellPosition: cellPosition, clockMinutes: clockMinutes, freshness: freshness,
    kpiYears: kpiYears, yearRows: yearRows, columnTotals: columnTotals, houseYear: houseYear, combine: combine,
    yearReservations: yearReservations, roleIndex: roleIndex, axisScale: axisScale
  };
})();
if (typeof module !== "undefined" && module.exports) { module.exports = CaseCalLogic; }
