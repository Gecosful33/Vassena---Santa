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
   * The guests in the house or still to come, from `today` on: the stays with guests
   * that have not ended yet, soonest first. Dates blocked without guests are not listed.
   * `current` marks the guests in the house on the night of `today`; `inDays` is how
   * far away the check-in is (0 for today, and for guests already there).
   */
  function upcomingGuests(prop, today) {
    if (prop.missing) return [];
    return prop.stays.filter(function (stay) {
      return stay.kind === "booking" && stay.end > today;
    }).map(function (stay) {
      return { stay: stay, current: stay.start <= today, inDays: Math.max(0, stay.start - today) };
    });
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
    return clipToWeek(stays.map(function (stay) {
      return { from: stay.start + inAt, to: stay.end + outAt, stay: stay };
    }), week).map(function (segment) {
      var stay = segment.span.stay;
      return { left: segment.left, right: segment.right, kind: stay.kind,
               cutLeft: segment.cutLeft, cutRight: segment.cutRight, stay: stay };
    });
  }

  /**
   * The part of each span that falls in one week row, in column units (0..7).
   * A span runs from `from` to `to`, both in days with the hour as a fraction.
   * cutLeft / cutRight say the span continues beyond this row.
   */
  function clipToWeek(spans, week) {
    var c0 = -1, c1 = -1;
    for (var c = 0; c < 7; c++) {
      if (week[c] !== null) { if (c0 < 0) c0 = c; c1 = c; }
    }
    var segments = [];
    if (c0 < 0) return segments;
    var d0 = week[c0], limit = week[c1] + 1;
    for (var i = 0; i < spans.length; i++) {
      var span = spans[i];
      var xs = Math.max(span.from, d0);
      var xe = Math.min(span.to, limit);
      if (xe <= xs) continue;
      segments.push({
        left: c0 + (xs - d0),
        right: c0 + (xe - d0),
        cutLeft: span.from < d0,
        cutRight: span.to > limit,
        span: span
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

  /* ---- the family's own entries ----
   * Who uses a house when, written by family members in the page (casecal/family.py).
   * An entry is a diary note, not a booking: it closes nothing on the manager's system.
   * Whether its nights are really blocked there is worked out here, against the portal data.
   * Entries come from a shared log and are validated like any other untrusted input.
   */

  var ENTRY_ID = /^[A-Za-z0-9_-]{8,40}$/, HOUSE_ID = /^[a-z][a-z0-9-]{0,30}$/;
  var YMD = /^\d{4}-\d{2}-\d{2}$/, STAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;
  var LIMITS = { who: 80, note: 200, by: 40 };
  var MAX_ENTRIES = 2000;
  // Bounds that keep a slip of the finger (year 2207, a ten-year stay) out of the diary.
  var MAX_NIGHTS = 366, FIRST_DAY = dayOf(2000, 1, 1), LAST_DAY = dayOf(2100, 1, 1);

  function tidyText(raw, limit, required) {
    if (raw === null || raw === undefined) raw = "";
    if (typeof raw !== "string") return null;
    var text = raw.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
    return text.length > limit || (required && !text) ? null : text;
  }
  function realDay(raw) {
    if (typeof raw !== "string" || !YMD.test(raw)) return null;
    var day = dayFromYmd(raw);
    return ymd(day) === raw ? day : null; // refuses 30 February
  }

  /** A well-formed entry in working shape (days as numbers), or null. */
  function validEntry(raw) {
    if (!raw || typeof raw !== "object") return null;
    if (typeof raw.id !== "string" || !ENTRY_ID.test(raw.id)) return null;
    if (typeof raw.prop !== "string" || !HOUSE_ID.test(raw.prop)) return null;
    var start = realDay(raw.start), end = realDay(raw.end);
    if (start === null || end === null || !(end > start)) return null;
    if (start < FIRST_DAY || end > LAST_DAY || end - start > MAX_NIGHTS) return null;
    var who = tidyText(raw.who, LIMITS.who, true), note = tidyText(raw.note, LIMITS.note, false);
    var by = tidyText(raw.by, LIMITS.by, false);
    if (who === null || note === null || by === null) return null;
    return {
      id: raw.id, prop: raw.prop, start: start, end: end, who: who, note: note, by: by,
      at: typeof raw.at === "string" && STAMP.test(raw.at) ? raw.at : ""
    };
  }

  /** The stored form of an entry: what a "set" event and the published page carry. */
  function entryRecord(entry) {
    return { id: entry.id, prop: entry.prop, start: ymd(entry.start), end: ymd(entry.end),
             who: entry.who, note: entry.note, by: entry.by, at: entry.at };
  }

  /**
   * Replay events, oldest first, over a map of entries (id -> entry) and return a new map.
   * "set" writes a whole entry, "del" removes one, "snap" replaces everything. A
   * malformed event is skipped. Replaying events the map already reflects is harmless,
   * so a change shown at once can be confirmed later by reading the log again.
   */
  function foldEvents(events, entries) {
    var state = {};
    Object.keys(entries || {}).forEach(function (id) { state[id] = entries[id]; });
    (events || []).forEach(function (event) {
      if (!event || typeof event !== "object") return;
      if (event.t === "set") {
        var entry = validEntry(event);
        if (entry && (state[entry.id] || Object.keys(state).length < MAX_ENTRIES)) state[entry.id] = entry;
      } else if (event.t === "del") {
        if (typeof event.id === "string") delete state[event.id];
      } else if (event.t === "snap" && Array.isArray(event.entries)) {
        var listed = event.entries.slice(0, MAX_ENTRIES).map(validEntry);
        if (listed.every(Boolean)) {
          state = {};
          listed.forEach(function (item) { state[item.id] = item; });
        }
      }
    });
    return state;
  }

  function entriesOf(entries, propId) {
    return Object.keys(entries).map(function (id) { return entries[id]; })
      .filter(function (entry) { return !propId || entry.prop === propId; })
      .sort(function (a, b) { return a.start - b.start || (a.id < b.id ? -1 : 1); });
  }

  /**
   * Where an entry stands against the manager's calendar.
   * state: "blocked"  every night is closed to sale on the portal: nobody can book over it;
   *        "pending"  some nights are still on sale: until the owner closes them, guests
   *                   can book them;
   *        "conflict" guests are booked on some of its nights (`guests` holds the first stay);
   *        "unknown"  it cannot be told: the house's portal could not be read, or it
   *                   publishes nothing yet for some of the nights.
   * `open` lists, as [start, end) runs, the nights not known to be closed: those on
   * sale and those the portal says nothing about.
   */
  function entryStatus(entry, prop) {
    if (!prop || prop.missing) return { state: "unknown", open: [], guests: null };
    var open = [], guests = null, run = null, onSale = false;
    for (var day = entry.start; day < entry.end; day++) {
      var info = dayInfo(prop, day);
      if (info.state === "booking") guests = guests || info.night;
      if (info.state === "booking" || info.state === "closed") { run = null; continue; }
      if (info.state === "free") onSale = true;
      if (run) run.end = day + 1;
      else { run = { start: day, end: day + 1 }; open.push(run); }
    }
    var state = guests ? "conflict" : onSale ? "pending" : open.length ? "unknown" : "blocked";
    return { state: state, open: open, guests: guests };
  }

  /**
   * Can this entry be saved? Returns { ok: true, shared } or { ok: false, code, detail }.
   * codes: "house", "who", "dates" (check-out not after check-in, or a date out of
   *        range), "long" (more than a year), "guests" (detail: the guests' stay).
   * Two family periods may share nights in a house: `shared` lists the other
   * periods on some of the same nights, so that the form can say so before saving.
   * `props` are the prepared houses, `entries` the current map; an entry being
   * edited is not listed against its own earlier version.
   */
  function checkEntry(candidate, props, entries) {
    var prop = null;
    props.forEach(function (item) { if (item.id === candidate.prop) prop = item; });
    if (!prop) return { ok: false, code: "house" };
    if (tidyText(candidate.who, LIMITS.who, true) === null) return { ok: false, code: "who" };
    if (typeof candidate.start !== "number" || typeof candidate.end !== "number" ||
        isNaN(candidate.start) || isNaN(candidate.end) || !(candidate.end > candidate.start) ||
        candidate.start < FIRST_DAY || candidate.end > LAST_DAY) {
      return { ok: false, code: "dates" };
    }
    if (candidate.end - candidate.start > MAX_NIGHTS) return { ok: false, code: "long" };
    var status = entryStatus(candidate, prop);
    if (status.state === "conflict") return { ok: false, code: "guests", detail: status.guests };
    return { ok: true, shared: entriesOf(entries, candidate.prop).filter(function (other) {
      return other.id !== candidate.id && other.start < candidate.end && candidate.start < other.end;
    }) };
  }

  var STANDING = ["blocked", "unknown", "pending", "conflict"]; // from fine to most urgent

  /**
   * The family's periods of one house as the calendar draws them: the stretches of
   * time during which the same periods are under way, each with those periods.
   * Where two periods share nights there is one stretch holding both, so the house
   * shows one bar with both names instead of two bars on top of each other.
   *
   * A period runs from the check-in hour of its first day to the check-out hour of
   * its last (`inAt`, `outAt`: fractions of a day), so `from` and `to` are days with
   * the hour as a fraction. `joinLeft` / `joinRight` say that the stretch touches
   * the one before or after it: someone arrives or leaves while someone else stays.
   * `state` is the most urgent standing among its periods (see entryStatus).
   */
  function familyRuns(entries, prop) {
    var inAt = prop.hours.inAt, outAt = prop.hours.outAt;
    var marks = [];
    entries.forEach(function (entry) { marks.push(entry.start + inAt, entry.end + outAt); });
    marks.sort(function (a, b) { return a - b; });
    var runs = [];
    for (var i = 0; i + 1 < marks.length; i++) {
      var from = marks[i], to = marks[i + 1];
      if (to - from < 1e-9) continue; // two periods beginning or ending at the same hour
      var active = entries.filter(function (entry) {
        return entry.start + inAt <= from + 1e-9 && to <= entry.end + outAt + 1e-9;
      });
      if (!active.length) continue;
      var worst = 0;
      active.forEach(function (entry) {
        worst = Math.max(worst, STANDING.indexOf(entryStatus(entry, prop).state));
      });
      var last = runs[runs.length - 1];
      var touching = !!last && Math.abs(last.to - from) < 1e-9;
      if (touching) last.joinRight = true;
      runs.push({ from: from, to: to, entries: active, state: STANDING[worst], joinLeft: touching, joinRight: false });
    }
    return runs;
  }

  /** "Famiglia Rossi" -> "Rossi": what is left of a name when a bar is short of room. */
  function shortName(who) {
    var short = who.replace(/^(famiglia|fam\.?)\s+/i, "");
    return short || who;
  }

  /**
   * The first day from `day` on whose night has neither guests nor a family period.
   * Falls back to `day` when the known data holds none.
   */
  function firstOpenDay(prop, day, entries) {
    var taken = entriesOf(entries, prop.id);
    function inUse(night) {
      return taken.some(function (entry) { return entry.start <= night && night < entry.end; });
    }
    for (var candidate = day; covered(prop, candidate); candidate++) {
      if (dayInfo(prop, candidate).state !== "booking" && !inUse(candidate)) return candidate;
    }
    return day;
  }

  /**
   * Dates to propose for a new period of `prop` that includes the night of `day`, as
   * { prop, start, end }. The proposal never runs into a family period already there.
   * On a night still for sale it is short: two nights, or one if a stay follows. Inside
   * dates blocked on the portal it is what is left of the block when that is short
   * enough to be one stay, otherwise a week of it from `day`.
   */
  function draftPeriod(prop, day, entries) {
    var floor = -Infinity, ceiling = Infinity;
    entriesOf(entries, prop.id).forEach(function (entry) {
      if (entry.end <= day) floor = Math.max(floor, entry.end);
      else if (entry.start > day) ceiling = Math.min(ceiling, entry.start);
    });
    var info = dayInfo(prop, day), start = day, end = day + 2;
    if (info.state === "closed") {
      var block = info.night;
      if (block.end - block.start <= 21) {
        start = Math.max(block.start, floor);
        end = block.end;
      } else {
        end = Math.min(day + 7, block.end);
      }
    } else {
      var status = nightStatus(prop, day);
      if (status.state === "free" && status.until !== null) end = Math.min(end, status.until);
    }
    return { prop: prop.id, start: start, end: Math.max(start + 1, Math.min(end, ceiling)) };
  }

  /** A new entry id from random bytes (kept short: it is typed by nobody). */
  function entryId(bytes) {
    var alphabet = "abcdefghijklmnopqrstuvwxyz0123456789", id = "e_";
    for (var i = 0; i < bytes.length; i++) id += alphabet[bytes[i] % alphabet.length];
    return id;
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

  /* ---- economics (the figures) ----
   * Every manager configures their own amount columns, so a house's owner entry
   * carries the column names of its monthly statistics (kpi_columns), the values row
   * by row, and the index of the few columns the page must recognise (kpi_roles).
   * No amount of a single booking is published: the figures are by month and by year.
   */

  function roleIndex(roles, name) {
    return roles && typeof roles[name] === "number" ? roles[name] : null;
  }
  function known(value) { return value !== null && value !== undefined; }

  function kpiYears(ownerProps) {
    var years = {};
    ownerProps.forEach(function (p) {
      (p.kpis || []).forEach(function (k) { years[k.y] = true; });
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

  /* ---- profit and loss ----
   * Income is what the portals report for the year. Costs are the yearly amounts the
   * owner keeps in a spreadsheet, one sheet per year (owner.costs): a list of lines,
   * each with an amount per house in cents, or nothing where it does not apply.
   */

  /** The cost sheets worth showing, oldest year first. Anything malformed is left out. */
  function costSheets(costs) {
    return (Array.isArray(costs) ? costs : []).filter(function (sheet) {
      return sheet && typeof sheet.year === "number" && Array.isArray(sheet.lines) && sheet.lines.length;
    }).sort(function (a, b) { return a.year - b.year; });
  }

  /**
   * The years every one of these houses is read for from its portal, whole: 1 January
   * to 31 December. Only for those can a year's income be set against its costs: a
   * year the portals are no longer (or not yet) read for would show an income of nothing.
   */
  function coveredYears(props) {
    var houses = props.filter(function (prop) { return !prop.missing; });
    if (!houses.length) return [];
    var first = Math.max.apply(null, houses.map(function (prop) { return prop.from; }));
    var last = Math.min.apply(null, houses.map(function (prop) { return prop.to; }));
    var years = [];
    for (var year = parts(first).y; dayOf(year + 1, 1, 1) <= last; year++) {
      if (dayOf(year, 1, 1) >= first) years.push(year);
    }
    return years;
  }

  /**
   * The year's result, house by house and all together.
   * `ownerProps` are the houses' figures (the readable ones, in page order) and
   * `sheet` the cost sheet of that year.
   *
   * Returns { houses: [column], total: column, lines: [{ label, note, amounts, total }] }.
   * A column holds: nights, adr_cents, gross_cents, withheld_cents (what the manager,
   * the channels and the flat tax keep: gross less net) with withheld_ratio, net_cents,
   * costs_cents, margin_cents (net less costs) with margin_ratio (of the net), and
   * breakeven_nights: how many nights at this year's net per night pay the costs.
   * In a line, `amounts` follow the houses; null says the cost does not apply there.
   */
  function profitAndLoss(ownerProps, sheet, year) {
    var lines = ((sheet && sheet.lines) || []).map(function (line) {
      var amounts = ownerProps.map(function (prop) {
        var value = line && line.amounts ? line.amounts[prop.id] : null;
        return typeof value === "number" && isFinite(value) ? value : null;
      });
      var total = null;
      amounts.forEach(function (value) { if (value !== null) total = (total || 0) + value; });
      return {
        label: typeof line.label === "string" ? line.label : "",
        note: typeof line.note === "string" ? line.note : "",
        amounts: amounts, total: total
      };
    }).filter(function (line) { return line.total !== null; });

    function column(figures, costs) {
      var nights = figures.nights || 0, gross = figures.gross_cents, net = figures.net_cents;
      var withheld = known(gross) && known(net) ? gross - net : null;
      var margin = known(net) ? net - costs : null;
      var perNight = known(net) && nights > 0 ? net / nights : null;
      return {
        nights: nights,
        adr_cents: known(gross) && nights > 0 ? Math.round(gross / nights) : null,
        gross_cents: known(gross) ? gross : null,
        withheld_cents: withheld,
        withheld_ratio: withheld !== null && gross > 0 ? withheld / gross : null,
        net_cents: known(net) ? net : null,
        costs_cents: costs,
        margin_cents: margin,
        margin_ratio: margin !== null && net > 0 ? margin / net : null,
        // Whole nights, and never the "-0" that rounding up a hair under nothing gives.
        breakeven_nights: perNight !== null && perNight > 0 ? Math.max(0, Math.ceil(costs / perNight - 1e-9)) : null
      };
    }

    // A house that sold no night that year earned nothing: its costs are a loss, not a
    // blank. One whose portal publishes no such column at all stays unknown.
    var years = ownerProps.map(function (prop) {
      var house = houseYear(prop, year);
      function or0(value, role) {
        return known(value) ? value : (!house.nights && roleIndex(prop.kpi_roles, role) !== null ? 0 : null);
      }
      return { nights: house.nights, gross_cents: or0(house.gross_cents, "adr"), net_cents: or0(house.net_cents, "net") };
    });
    var houses = years.map(function (figures, i) {
      var costs = 0;
      lines.forEach(function (line) { costs += line.amounts[i] || 0; });
      return column(figures, costs);
    });
    var all = combine(years, year);
    var allCosts = 0;
    houses.forEach(function (house) { allCosts += house.costs_cents; });
    return { houses: houses, total: column(all, allCosts), lines: lines };
  }

  return {
    DAY_MS: DAY_MS,
    dayOf: dayOf, dayFromYmd: dayFromYmd, parts: parts, ymd: ymd,
    daysInMonth: daysInMonth, daysInYear: daysInYear, todayInZone: todayInZone,
    prepare: prepare, badges: badges, covered: covered, dayInfo: dayInfo, nightStatus: nightStatus,
    upcomingGuests: upcomingGuests, monthsBetween: monthsBetween, monthWeeks: monthWeeks,
    weekSegments: weekSegments, clipToWeek: clipToWeek, cellPosition: cellPosition, clockMinutes: clockMinutes,
    freshness: freshness, familyRuns: familyRuns, shortName: shortName,
    validEntry: validEntry, entryRecord: entryRecord, foldEvents: foldEvents, entriesOf: entriesOf,
    entryStatus: entryStatus, checkEntry: checkEntry, entryId: entryId, MAX_NIGHTS: MAX_NIGHTS,
    firstOpenDay: firstOpenDay, draftPeriod: draftPeriod,
    kpiYears: kpiYears, yearRows: yearRows, columnTotals: columnTotals, houseYear: houseYear, combine: combine,
    roleIndex: roleIndex, axisScale: axisScale,
    costSheets: costSheets, coveredYears: coveredYears, profitAndLoss: profitAndLoss
  };
})();
if (typeof module !== "undefined" && module.exports) { module.exports = CaseCalLogic; }
