/* DOM layer: lock screen, calendar, stays and owner area.
 * Labels come from the managers' portals and are therefore untrusted: every
 * string is inserted with textContent, never parsed as markup. Positions are
 * set through the CSSOM because the page's CSP forbids inline style attributes. */
(function () {
  "use strict";

  var C = CaseCalCrypto, L = CaseCalLogic, Store = CaseCalStore;
  var SVG_NS = "http://www.w3.org/2000/svg";
  var STORE = "casecal.keys.v1";
  var GUESTS_SHOWN = 4;
  var IT = "it-IT";

  var root = document.getElementById("app");
  var envelope = JSON.parse(document.getElementById("casecal-data").textContent);
  var genericTitle = document.title;
  var state = null;

  function blankState() {
    return {
      // `family` is the calendar, `owner` the figures: both open with the one password.
      // `house` is the house on show: the page deals with one at a time.
      // `pnl` says that the profit and loss is on show instead of a house.
      family: null, owner: null, props: [], today: 0, house: null, year: null, pnl: false, pnlYear: null,
      showPast: false, pastStays: {}, allGuests: {}, remembered: false,
      // The family's own entries: the current map, where changes are written (null =
      // read-only page), how far the log has been read, and the key that opens it.
      entries: {}, store: null, cursor: 0, familyKey: null,
      sync: { phase: "off", code: null },
      groups: [] // the families the entry form offers as fixed choices (empty: free text)
    };
  }

  /* ---------- small DOM helpers ---------- */

  function append(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(node, c); }); return; }
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }

  function el(tag, attrs) {
    var node = document.createElement(tag);
    for (var name in attrs || {}) {
      var value = attrs[name];
      if (value === null || value === undefined || value === false) continue;
      if (name === "class") node.className = value;
      else if (name === "text") node.textContent = value;
      else if (name === "checked") node.checked = true;
      else if (name.slice(0, 2) === "on") node.addEventListener(name.slice(2), value);
      else node.setAttribute(name, value === true ? "" : value);
    }
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  var ICONS = {
    free: "M3 8.5l3.2 3.2L13 5",
    booking: "M4.5 7.5h7V13h-7zM6 7.5V5.6a2 2 0 0 1 4 0v1.9",
    lock: "M4.5 7.5h7V13h-7zM6 7.5V5.6a2 2 0 0 1 4 0v1.9",
    closed: "M8 2.5a5.5 5.5 0 1 0 0 11a5.5 5.5 0 1 0 0-11zM5.2 8h5.6",
    unknown: "M6 6.2a2 2 0 1 1 3.1 1.7c-.7.5-1.1.9-1.1 1.8M8 12.3v.2",
    warning: "M8 2.5l6 10.5H2zM8 6.6v2.9M8 11.4v.2",
    critical: "M8 2.5l6 10.5H2zM8 6.6v2.9M8 11.4v.2",
    info: "M8 2.5a5.5 5.5 0 1 0 0 11a5.5 5.5 0 1 0 0-11zM8 7.4v3.4M8 5.2v.2"
  };

  function icon(name) {
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("class", "ico ico-" + name);
    svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", ICONS[name]);
    svg.appendChild(path);
    return svg;
  }

  /* ---------- formatting (Italian) ---------- */

  function dtf(options) { return new Intl.DateTimeFormat(IT, options); }
  var F = {
    day: dtf({ timeZone: "UTC", weekday: "short", day: "numeric", month: "short" }),
    dayYear: dtf({ timeZone: "UTC", weekday: "short", day: "numeric", month: "short", year: "numeric" }),
    short: dtf({ timeZone: "UTC", day: "numeric", month: "short" }),
    shortYear: dtf({ timeZone: "UTC", day: "numeric", month: "short", year: "numeric" }),
    long: dtf({ timeZone: "UTC", weekday: "long", day: "numeric", month: "long", year: "numeric" }),
    written: dtf({ timeZone: "UTC", day: "numeric", month: "long", year: "numeric" }),
    month: dtf({ timeZone: "UTC", month: "long", year: "numeric" }),
    monthName: dtf({ timeZone: "UTC", month: "long" }),
    monthShort: dtf({ timeZone: "UTC", month: "short" }),
    // Italian omits the thousands separator on four-digit numbers by default; in a
    // table of amounts "1.250,00" lines up and reads better than "1250,00".
    money: new Intl.NumberFormat(IT, { style: "currency", currency: "EUR", useGrouping: "always" }),
    money0: new Intl.NumberFormat(IT, { style: "currency", currency: "EUR", maximumFractionDigits: 0, useGrouping: "always" }),
    pct: new Intl.NumberFormat(IT, { style: "percent", maximumFractionDigits: 0 }),
    pct1: new Intl.NumberFormat(IT, { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1 }),
    // For a column of amounts whose unit is said once, above the table.
    amount: new Intl.NumberFormat(IT, { minimumFractionDigits: 2, maximumFractionDigits: 2, useGrouping: "always" }),
    int: new Intl.NumberFormat(IT, { useGrouping: "always" })
  };
  var DOW = ["L", "M", "M", "G", "V", "S", "D"];

  function dateOf(day) { return new Date(day * L.DAY_MS); }
  function sameYear(day) { return L.parts(day).y === L.parts(state.today).y; }
  function dayLabel(day) { return (sameYear(day) ? F.day : F.dayYear).format(dateOf(day)); }
  /** The first day of a stay, beside its last: a year the last day will say is not said twice. */
  function startLabel(stay) {
    return L.parts(stay.start).y === L.parts(stay.end).y ? F.day.format(dateOf(stay.start)) : dayLabel(stay.start);
  }
  function shortLabel(day) { return (sameYear(day) ? F.short : F.shortYear).format(dateOf(day)); }
  function nights(n) { return n === 1 ? "1 notte" : F.int.format(n) + " notti"; }
  /** "gennaio" -> "Gennaio": Italian writes month names small, a table's first column does not. */
  function capital(text) { return text.charAt(0).toLocaleUpperCase(IT) + text.slice(1); }
  function euro(cents) { return cents === null || cents === undefined ? "–" : F.money.format(cents / 100); }
  function pct(ratio) { return ratio === null || ratio === undefined ? "–" : F.pct.format(ratio); }

  /** A timestamp in words. `genitive` gives "di ieri alle…" / "del 18 novembre…". */
  function stamp(ms, genitive) {
    var zone = state.family.tz;
    var when = new Date(ms);
    var time = dtf({ timeZone: zone, hour: "2-digit", minute: "2-digit" }).format(when);
    var day = L.todayInZone(zone, ms);
    if (day === state.today) return (genitive ? "di oggi alle " : "oggi alle ") + time;
    if (day === state.today - 1) return (genitive ? "di ieri alle " : "ieri alle ") + time;
    return (genitive ? "del " : "il ") +
      dtf({ timeZone: zone, day: "numeric", month: "long", year: "numeric" }).format(when) + " alle " + time;
  }

  /* ---------- remembered keys ---------- */

  function loadKeys() {
    try {
      var keys = JSON.parse(localStorage.getItem(STORE) || "null");
      return keys && typeof keys === "object" ? keys : null;
    } catch (err) { return null; }
  }
  function saveKeys(keys) { try { localStorage.setItem(STORE, JSON.stringify(keys)); } catch (err) { /* private mode */ } }
  function clearKeys() { try { localStorage.removeItem(STORE); } catch (err) { /* private mode */ } }

  /* ---------- lock screen ---------- */

  function passwordField(id, labelText, autocomplete) {
    var input = el("input", {
      class: "input", id: id, type: "password", required: true, autocomplete: autocomplete,
      autocapitalize: "none", autocorrect: "off", spellcheck: "false"
    });
    var toggle = el("button", { class: "btn", type: "button", "aria-pressed": "false", text: "Mostra" });
    toggle.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      toggle.textContent = show ? "Nascondi" : "Mostra";
      toggle.setAttribute("aria-pressed", String(show));
    });
    return {
      input: input,
      node: el("div", { class: "field" }, el("label", { for: id, text: labelText }),
        el("div", { class: "pw-row" }, input, toggle))
    };
  }

  function showError(node, message) {
    clear(node);
    if (!message) { node.hidden = true; return; }
    append(node, [icon("critical"), el("span", { text: message })]);
    node.hidden = false;
  }

  function renderLock(notice) {
    document.title = genericTitle;
    var field = passwordField("pw", "Password", "current-password");
    var remember = el("input", { type: "checkbox", checked: true });
    var error = el("p", { class: "error", role: "alert", hidden: true });
    var submit = el("button", { class: "btn primary", type: "submit", text: "Apri il calendario" });
    var form = el("form", { class: "lock-card", novalidate: true },
      el("h1", { text: genericTitle }),
      el("p", { text: notice || "Inserisci la password del calendario." }),
      field.node,
      el("label", { class: "check" }, remember, "Ricorda su questo dispositivo"),
      error, submit);

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!field.input.value.trim()) { showError(error, "Scrivi la password."); return; }
      submit.disabled = true;
      submit.textContent = "Verifica in corso…";
      showError(error, "");
      C.deriveKey(field.input.value, envelope.kdf).then(function (key) {
        return C.unlock(envelope, key);
      }).then(function (opened) {
        if (!opened) throw new Error("Password non corretta.");
        if (remember.checked) {
          // Kept under the name it had when the figures had a password of their own:
          // a device that remembered the calendar's key then still opens the page now.
          saveKeys({ salt: envelope.kdf.salt, family: C.bytesToB64(opened.key) });
        } else {
          clearKeys();
        }
        enter(opened, remember.checked);
      }).catch(function (err) {
        submit.disabled = false;
        submit.textContent = "Apri il calendario";
        showError(error, err && err.message ? err.message : "Impossibile aprire il calendario.");
        field.input.select();
      });
    });

    clear(root);
    root.appendChild(el("main", { class: "lock" }, form));
    field.input.focus();
  }

  function lock() {
    clearKeys();
    hideTip();
    dropSheet();
    state = blankState();
    renderLock();
  }

  function enter(opened, remembered) {
    state = blankState();
    state.family = opened.family;
    state.owner = opened.owner;
    state.remembered = remembered;
    state.props = opened.family.properties.map(L.prepare);
    // The mark stands in for the photo a booking app would show on each bar.
    var marks = L.badges(state.props.map(function (prop) { return prop.label; }));
    // The photo itself, where the house has one, is on its tab and at the top of its view.
    var photos = L.photoUrls(opened.family.photos);
    state.props.forEach(function (prop, index) {
      prop.index = index;
      prop.badge = marks[index];
      prop.photo = photos[prop.id] || null;
    });
    state.today = L.todayInZone(opened.family.tz, Date.now());
    document.title = opened.family.title || genericTitle;
    // The house this device looked at last, or the first one.
    var last = lastHouse();
    state.house = state.props.some(function (prop) { return prop.id === last; }) ? last
      : (state.props.length ? state.props[0].id : null);

    // What the nightly build already knew, then whatever has been written since.
    state.familyKey = opened.key;
    state.entries = L.foldEvents((opened.family.entries || []).map(function (raw) {
      return Object.assign({ t: "set" }, raw);
    }), {});
    // Names come from a setting typed by hand: only plain, short, distinct ones are offered.
    (Array.isArray(opened.family.groups) ? opened.family.groups : []).forEach(function (name) {
      if (typeof name !== "string" || !name.trim() || name.length > 80) return;
      if (state.groups.indexOf(name.trim()) < 0) state.groups.push(name.trim());
    });
    var log = opened.family.log;
    state.store = Store.open(log, { storage: browserStorage() });
    state.cursor = log && typeof log.cursor === "number" ? log.cursor : 0;
    state.sync = { phase: state.store ? "loading" : "off", code: null };
    render();
    refreshEntries();
  }

  /* ---------- tooltip ---------- */

  var tip = null, tipAnchor = null, lastPointer = "mouse";

  /** Keep the readout next to its mark; drop it once the mark leaves the viewport. */
  function placeTip() {
    if (!tip || tip.hidden || !tipAnchor) return;
    var box = tipAnchor.getBoundingClientRect();
    var viewWidth = document.documentElement.clientWidth, viewHeight = window.innerHeight;
    if (box.bottom < 0 || box.top > viewHeight || box.right < 0 || box.left > viewWidth) { hideTip(); return; }
    var width = tip.offsetWidth, height = tip.offsetHeight;
    var left = Math.min(Math.max(8, box.left + box.width / 2 - width / 2), viewWidth - width - 8);
    var top = box.top - height - 8;
    if (top < 8) top = Math.min(box.bottom + 8, viewHeight - height - 8);
    tip.style.left = Math.round(left) + "px";
    tip.style.top = Math.round(Math.max(8, top)) + "px";
  }

  function showTip(anchor, content) {
    if (!tip) {
      tip = el("div", { class: "tip", "aria-hidden": "true" });
      document.body.appendChild(tip);
    }
    clear(tip);
    append(tip, content);
    tip.hidden = false;
    if (tipAnchor && tipAnchor !== anchor) tipAnchor.classList.remove("active");
    tipAnchor = anchor;
    anchor.classList.add("active");
    placeTip();
  }

  function hideTip() {
    if (tip) tip.hidden = true;
    if (tipAnchor) tipAnchor.classList.remove("active");
    tipAnchor = null;
  }

  /** Did the keyboard move the focus here? A click, a tap or a closing sheet do not count. */
  function keyboardFocus(node) {
    try { return node.matches(":focus-visible"); } catch (err) { return true; } // older browsers
  }

  /**
   * Hover and keyboard focus open the readout of the marks inside `container`; so does
   * a tap, unless `tapOpens` is false because a tap does something else there.
   */
  function attachTips(container, selector, build, tapOpens) {
    function target(event) { return event.target.closest ? event.target.closest(selector) : null; }
    container.addEventListener("pointerover", function (event) {
      var node = target(event);
      if (node && event.pointerType === "mouse") showTip(node, build(node));
    });
    container.addEventListener("pointerout", function (event) {
      if (event.pointerType === "mouse") hideTip();
    });
    container.addEventListener("focusin", function (event) {
      var node = target(event);
      if (node && keyboardFocus(node)) showTip(node, build(node));
    });
    container.addEventListener("focusout", hideTip);
    if (tapOpens === false) return;
    container.addEventListener("click", function (event) {
      var node = target(event);
      if (!node) return;
      // A second tap on the same mark closes its readout. "Same" is judged by what was
      // open when the finger went down.
      if (lastPointer !== "mouse" && openAtPointerDown === node) hideTip();
      else showTip(node, build(node));
    });
  }

  var openAtPointerDown = null;
  document.addEventListener("pointerdown", function (event) {
    lastPointer = event.pointerType || "mouse";
    openAtPointerDown = tip && !tip.hidden ? tipAnchor : null;
    var inside = event.target.closest && event.target.closest(".day, .group");
    if (tipAnchor && !inside) hideTip();
  });
  document.addEventListener("keydown", function (event) {
    openAtPointerDown = null;
    if (event.key === "Escape") hideTip();
  });
  // Focusing a day with the keyboard scrolls it into view: follow it instead of closing.
  window.addEventListener("scroll", placeTip, { passive: true });
  window.addEventListener("resize", placeTip);
  // Bars change width with the window, so what fits inside them does too.
  var refit = 0;
  window.addEventListener("resize", function () {
    if (!state || !state.family) return;
    cancelAnimationFrame(refit);
    refit = requestAnimationFrame(fitLabels);
  });

  /* ---------- shared pieces ---------- */

  /** The house on show (a list, because the sections are written for any number of houses). */
  function visible() {
    return state.props.filter(function (prop) { return prop.id === state.house; });
  }
  function readable() { return visible().filter(function (prop) { return !prop.missing; }); }

  function segmented(name, label, options, current, onPick) {
    return el("div", { class: "seg", role: "radiogroup", "aria-label": label }, options.map(function (option) {
      var input = el("input", {
        type: "radio", name: name, value: String(option.id), "data-key": name + ":" + option.id,
        checked: option.id === current
      });
      input.addEventListener("change", function () { onPick(option.id, name + ":" + option.id); });
      // A house is told by its photo where it has one (ringed in its colour), by its colour alone otherwise.
      var mark = option.index === undefined ? null
        : option.photo ? el("img", { class: "thumb", src: option.photo, alt: "", decoding: "sync" }) : el("i", { class: "swatch" });
      var face = el("span", option.index === undefined ? null : { class: "p" + option.index }, mark, option.label);
      return el("label", null, input, face);
    }));
  }

  // "Bloccata" = dates closed to sale by the owner or the manager, with nobody booked.
  var STATE_TEXT = { free: "Libera", booking: "Ospiti", closed: "Bloccata", unknown: "Dato non disponibile" };

  function guestsLabel(count) { return count === 1 ? "1 ospite" : count + " ospiti"; }
  /** "Airbnb · 4 ospiti", or whatever part of it the portal provides. */
  function stayDetails(stay) {
    var parts = [];
    if (stay.channel) parts.push(stay.channel);
    if (stay.guests !== null && stay.guests !== undefined) parts.push(guestsLabel(stay.guests));
    return parts.join(" · ");
  }

  /**
   * Words for one house on one calendar day (tooltip and screen reader share them).
   * Hours are spelled out: a stay runs from the house's check-in time on its first day
   * to its check-out time on its last, which is what the bars draw.
   */
  function describe(prop, day) {
    var info = L.dayInfo(prop, day), hours = prop.hours;
    var detail = "";
    if (info.state === "free") {
      var status = L.nightStatus(prop, day);
      var parts = [];
      if (info.leaving) parts.push("dalle " + hours.checkOut + " (check-out al mattino)");
      parts.push(status.open ? "nessuna prenotazione successiva"
        : "fino alle " + hours.checkIn + " di " + dayLabel(status.until) + ", " + nights(status.until - day));
      detail = parts.join(", ");
    } else if (info.state === "booking" || info.state === "closed") {
      var stay = info.night;
      detail = "dalle " + hours.checkIn + " di " + dayLabel(stay.start) + " alle " + hours.checkOut + " di " +
        dayLabel(stay.end) + ", " + nights(stay.end - stay.start);
      if (stayDetails(stay)) detail += ", " + stayDetails(stay);
      if (stay.start === day && info.leaving) {
        detail += ". Cambio in giornata: uscita entro le " + hours.checkOut + ", ingresso dalle " + hours.checkIn;
      }
    }
    return { status: STATE_TEXT[info.state], detail: detail };
  }

  /* ---------- the family's own entries ----------
   * Anyone who can open the page can say who uses a house and when. Entries are a
   * diary, not bookings: nothing is closed on the manager's system, and every entry
   * says whether its nights are really blocked there.
   */

  var ME = "casecal.me", MINE = "casecal.family", HOUSE = "casecal.house";
  var STORE_MESSAGES = {
    offline: "Connessione assente: le modifiche non si possono leggere né salvare.",
    token: "Il token del registro di famiglia è scaduto o è stato revocato: finché non viene rinnovato le modifiche non sono disponibili.",
    denied: "GitHub ha rifiutato la richiesta: il token del registro di famiglia non ha più i permessi necessari, oppure il registro è stato rimosso.",
    limit: "Troppe richieste a GitHub in poco tempo: riprova tra qualche minuto.",
    server: "GitHub non ha risposto correttamente: riprova tra poco."
  };
  function storeMessage(error) { return STORE_MESSAGES[error && error.code] || STORE_MESSAGES.server; }

  function browserStorage() { try { return window.localStorage; } catch (err) { return null; } }
  function myName() { try { return localStorage.getItem(ME) || ""; } catch (err) { return ""; } }
  function rememberName(name) { try { localStorage.setItem(ME, name); } catch (err) { /* private mode */ } }
  // The family this device last wrote a period for: the next one starts from there.
  function myFamily() { try { return localStorage.getItem(MINE) || ""; } catch (err) { return ""; } }
  function rememberFamily(name) { try { localStorage.setItem(MINE, name); } catch (err) { /* private mode */ } }
  // The house this device looked at last: the page opens on it.
  function lastHouse() { try { return localStorage.getItem(HOUSE) || ""; } catch (err) { return ""; } }
  function rememberHouse(id) { try { localStorage.setItem(HOUSE, id); } catch (err) { /* private mode */ } }

  function familyView() {
    return JSON.stringify(L.entriesOf(state.entries).map(L.entryRecord)) + state.sync.phase + state.sync.code;
  }
  function focusedKey() {
    return document.activeElement && document.activeElement.getAttribute
      ? document.activeElement.getAttribute("data-key") : null;
  }

  var refreshing = null;
  /** Read what has been written to the log since the page was built or last read. */
  function refreshEntries() {
    if (!state || !state.store) return Promise.resolve();
    if (refreshing) return refreshing;
    var session = state, shown = familyView();
    refreshing = session.store.list(session.cursor).then(function (comments) {
      return Promise.all(comments.map(function (comment) {
        return C.decodeEvent(session.familyKey, comment.body);
      })).then(function (events) {
        session.entries = L.foldEvents(events.filter(Boolean), session.entries);
        if (comments.length) session.cursor = comments[comments.length - 1].id;
        session.sync = { phase: "ready", code: null };
      });
    }).catch(function (error) {
      session.sync = { phase: "error", code: (error && error.code) || "server" };
    }).then(function () {
      refreshing = null;
      // Redraw only if something changed, and never for a page that has since been locked.
      if (state === session && familyView() !== shown) render(focusedKey());
    });
    return refreshing;
  }

  /** Write one change; it shows at once and is then confirmed by reading the log again. */
  function saveEvent(event) {
    var session = state;
    return C.encodeEvent(session.familyKey, event).then(function (body) {
      return session.store.append(body);
    }).then(function () {
      session.entries = L.foldEvents([event], session.entries);
      session.sync = { phase: "ready", code: null };
      if (state !== session) return;
      render();
      // A read already under way may have started before this write: let it finish,
      // then read again, so that the log's own order has the last word.
      (refreshing || Promise.resolve()).then(refreshEntries);
    });
  }

  document.addEventListener("visibilitychange", function () { if (!document.hidden) refreshEntries(); });
  setInterval(function () { if (!document.hidden) refreshEntries(); }, 90000);

  /* A sheet is a modal dialog laid over the page; it lives outside the redrawn area.
   * The page may be redrawn while a sheet is open, so what opened it is remembered
   * by key, not by node, and gets the focus back when the sheet closes. */
  var sheet = null, sheetOpener = null;

  function dropSheet() {
    var open = sheet;
    sheet = null;
    sheetOpener = null;
    if (!open) return;
    if (open.open) open.close();
    open.remove();
  }

  function closeSheet() {
    var opener = sheetOpener;
    dropSheet();
    focusByKey(opener);
  }

  function openSheet(title, body) {
    // A sheet opened from another one returns to where the first was opened.
    var opener = sheet ? sheetOpener : focusedKey();
    dropSheet();
    hideTip();
    var dialog = el("dialog", { class: "sheet", "aria-label": title },
      el("div", { class: "sheet-head" }, el("h2", { text: title }),
        el("button", { class: "btn quiet", type: "button", text: "Chiudi", onclick: closeSheet })),
      body);
    dialog.addEventListener("close", function () { if (sheet === dialog) closeSheet(); }); // Esc
    dialog.addEventListener("click", function (event) { if (event.target === dialog) closeSheet(); }); // backdrop
    document.body.appendChild(dialog);
    sheet = dialog;
    sheetOpener = opener;
    dialog.showModal();
  }

  var ENTRY_STATE = {
    blocked: { icon: "free", text: "Bloccata sul portale" },
    pending: { icon: "warning", text: "Da far bloccare al gestore" },
    conflict: { icon: "critical", text: "Ci sono ospiti in quelle date" },
    unknown: { icon: "unknown", text: "Non verificabile sul portale" }
  };

  function runsText(runs) {
    return runs.map(function (run) { return shortLabel(run.start) + " → " + shortLabel(run.end); }).join(", ");
  }

  function entryStateLine(status) {
    var look = ENTRY_STATE[status.state];
    var extra = status.state === "pending" ? "ancora in vendita: " + runsText(status.open)
      : status.state === "conflict" ? "da " + dayLabel(status.guests.start) + " a " + dayLabel(status.guests.end)
      : status.state === "unknown" && status.open.length ? "il portale non pubblica ancora queste date: " + runsText(status.open)
      : status.state === "unknown" ? "il portale di questa casa non è stato letto"
      : "";
    return el("p", { class: "entry-state" }, icon(look.icon),
      el("span", null, el("strong", { text: look.text }), extra ? el("span", { class: "sub", text: extra }) : null));
  }

  function entryItem(entry, prop) {
    return el("li", { class: "entry" },
      el("div", { class: "entry-who" }, el("strong", { text: entry.who }),
        entry.by ? el("span", { class: "sub", text: "segnato da " + entry.by }) : null),
      el("div", { class: "entry-dates", text: dayLabel(entry.start) + " → " + dayLabel(entry.end) +
        " · " + nights(entry.end - entry.start) }),
      entry.note ? el("div", { class: "entry-note", text: entry.note }) : null,
      entryStateLine(L.entryStatus(entry, prop)),
      state.store ? el("button", {
        class: "btn", type: "button", "data-key": "entry:" + entry.id, text: "Modifica",
        onclick: function () { entrySheet(entry, null); }
      }) : null);
  }

  function houseById(id) {
    return state.props.filter(function (prop) { return prop.id === id; })[0] || null;
  }

  function field(id, labelText, control) {
    return el("div", { class: "field" }, el("label", { for: id, text: labelText }), control);
  }

  /** The form that adds an entry (`entry` null, `draft` gives house and dates) or changes one. */
  function entrySheet(entry, draft) {
    var start = entry || draft;
    // Every house, also one whose portal could not be read: the diary does not depend on it.
    var house = el("select", { class: "input", id: "entry-house" }, state.props.map(function (prop) {
      var option = el("option", { value: prop.id, text: prop.label });
      option.selected = prop.id === start.prop;
      return option;
    }));
    var from = el("input", {
      class: "input", id: "entry-from", type: "date", required: true, min: "2000-01-01", max: "2099-12-31",
      value: L.ymd(start.start)
    });
    var to = el("input", {
      class: "input", id: "entry-to", type: "date", required: true, min: "2000-01-02", max: "2100-01-01",
      value: L.ymd(start.end)
    });
    var groups = state.groups, OTHER = "\u0000altro"; // a value no name can have
    var known = {};
    L.entriesOf(state.entries).forEach(function (other) {
      if (groups.indexOf(other.who) < 0) known[other.who] = true;
    });
    // With a list of families the choice is one of them or "Altro"; a new period
    // starts from the family this device wrote for last time.
    var picked = !groups.length ? OTHER
      : entry ? (groups.indexOf(entry.who) >= 0 ? entry.who : OTHER)
      : (groups.indexOf(myFamily()) >= 0 ? myFamily() : null);
    var who = el("input", {
      class: "input", id: "entry-who", type: "text", maxlength: "80", autocomplete: "off",
      list: "entry-people", value: entry && picked === OTHER ? entry.who : "",
      placeholder: groups.length ? "Scrivi chi usa la casa" : "Es. Marco e Giulia",
      "aria-label": groups.length ? "Chi usa la casa: altro" : null
    });
    var choices = groups.length ? el("div", { class: "choices", role: "radiogroup", "aria-labelledby": "entry-who-label" },
      groups.concat([OTHER]).map(function (value, index) {
        var input = el("input", { type: "radio", name: "entry-who-choice", id: "entry-who-" + index });
        input.checked = value === picked;
        input.addEventListener("change", function () {
          picked = value;
          who.hidden = picked !== OTHER;
          if (picked === OTHER) who.focus();
          refreshPreview();
        });
        return el("label", null, input, el("span", { text: value === OTHER ? "Altro" : value }));
      })) : null;
    who.hidden = picked !== OTHER;
    function whoValue() { return picked === OTHER ? who.value : (picked || ""); }
    var note = el("input", {
      class: "input", id: "entry-note", type: "text", maxlength: "200", autocomplete: "off",
      value: entry ? entry.note : ""
    });
    var me = el("input", {
      class: "input", id: "entry-me", type: "text", maxlength: "40", required: true, autocomplete: "given-name",
      value: myName()
    });
    var preview = el("p", { class: "preview", "aria-live": "polite" });
    var sharing = el("p", { class: "preview", "aria-live": "polite" });
    var error = el("p", { class: "error", role: "alert", hidden: true });
    var submit = el("button", { class: "btn primary", type: "submit", text: entry ? "Salva le modifiche" : "Salva" });
    var remove = entry ? el("button", { class: "btn danger", type: "button", text: "Elimina" }) : null;

    function candidate() {
      return {
        id: entry ? entry.id : null, prop: house.value, who: whoValue(),
        start: from.value ? L.dayFromYmd(from.value) : NaN, end: to.value ? L.dayFromYmd(to.value) : NaN
      };
    }
    function explain(check) {
      if (check.code === "who") return groups.length && picked === null ? "Scegli chi usa la casa." : "Scrivi chi usa la casa.";
      if (check.code === "dates") return "Controlla le date: il check-out deve essere dopo il check-in.";
      if (check.code === "long") {
        return "Un periodo può durare al massimo " + L.MAX_NIGHTS + " notti: per un uso più lungo inserisci più periodi.";
      }
      if (check.code === "guests") {
        return "In quelle date ci sono ospiti (da " + dayLabel(check.detail.start) + " a " + dayLabel(check.detail.end) + "): scegli altre date.";
      }
      return "Scegli una casa.";
    }
    /** Said before saving, not to stop it: who else has the house on some of those nights. */
    function sharedWith(others) {
      var listed = others.slice(0, 3).map(function (other) {
        return other.who + " (" + shortLabel(other.start) + " → " + shortLabel(other.end) + ")";
      });
      return "In quelle date c'è anche " + listed.join(", ") + (others.length > 3 ? " e altri" : "") + ": la casa sarà condivisa.";
    }
    function refreshPreview() {
      clear(preview);
      clear(sharing);
      var asked = candidate(), prop = houseById(asked.prop);
      if (!prop || isNaN(asked.start) || isNaN(asked.end)) return;
      // The name is checked on saving, not while the dates are being chosen.
      var check = L.checkEntry(Object.assign({}, asked, { who: asked.who.trim() || "-" }), state.props, state.entries);
      if (!check.ok) {
        append(preview, [icon("critical"), el("span", { text: explain(check) })]);
        return;
      }
      if (check.shared.length) append(sharing, [icon("info"), el("span", { text: sharedWith(check.shared) })]);
      var status = L.entryStatus(asked, prop);
      var when = "Dalle " + prop.hours.checkIn + " di " + dayLabel(asked.start) + " alle " + prop.hours.checkOut +
        " di " + dayLabel(asked.end) + ", " + nights(asked.end - asked.start) + ". ";
      if (status.state === "blocked") {
        append(preview, [icon("free"), el("span", null, when,
          el("strong", { text: "Date già bloccate sul portale:" }), " nessun ospite può prenotarle.")]);
      } else if (status.state === "unknown") {
        append(preview, [icon("unknown"), el("span", null, when,
          el("strong", { text: "Non verificabile:" }), status.open.length
            ? " il portale non pubblica ancora queste date (" + runsText(status.open) + "). Vanno controllate lì."
            : " il portale di questa casa non è stato letto. Le date vanno controllate lì.")]);
      } else {
        append(preview, [icon("warning"), el("span", null, when,
          el("strong", { text: "Date non ancora bloccate" }),
          " (" + runsText(status.open) + "): finché il gestore non le blocca, gli ospiti possono prenotarle.")]);
      }
    }
    function busy(on) {
      submit.disabled = on;
      if (remove) remove.disabled = on;
    }
    function fail(problem) {
      busy(false);
      showError(error, storeMessage(problem));
    }
    function stampNow() { return new Date().toISOString().replace(/\.\d+Z$/, "Z"); }

    from.addEventListener("change", function () {
      // Moving the check-in past the check-out keeps at least one night.
      var asked = candidate();
      if (!isNaN(asked.start) && !(asked.end > asked.start)) to.value = L.ymd(asked.start + 1);
    });
    [house, from, to].forEach(function (control) { control.addEventListener("change", refreshPreview); });
    [from, to].forEach(function (control) { control.addEventListener("input", refreshPreview); });

    var form = el("form", { class: "sheet-body form", novalidate: true },
      field("entry-house", "Casa", house),
      el("div", { class: "pair" }, field("entry-from", "Check-in", from), field("entry-to", "Check-out", to)),
      preview,
      groups.length
        ? el("div", { class: "field" }, el("span", { class: "label", id: "entry-who-label", text: "Chi usa la casa" }), choices, who)
        : field("entry-who", "Chi usa la casa", who),
      sharing,
      el("datalist", { id: "entry-people" }, Object.keys(known).sort().map(function (name) {
        return el("option", { value: name });
      })),
      field("entry-note", "Nota (facoltativa)", note),
      field("entry-me", "Il tuo nome", me),
      el("p", { class: "hint", text: "È un promemoria per la famiglia: le date le blocca il gestore, su richiesta." }),
      error,
      el("div", { class: "sheet-actions" }, submit, remove));

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      showError(error, "");
      var asked = candidate();
      var check = L.checkEntry(asked, state.props, state.entries);
      if (!check.ok) { showError(error, explain(check)); return; }
      var author = me.value.trim();
      if (!author) { showError(error, "Scrivi il tuo nome: serve agli altri per sapere chi ha segnato il periodo."); me.focus(); return; }
      // Only what the readers of the log accept is ever written to it.
      var record = L.validEntry({
        id: asked.id || L.entryId(crypto.getRandomValues(new Uint8Array(12))), prop: asked.prop,
        start: L.ymd(asked.start), end: L.ymd(asked.end),
        who: asked.who, note: note.value, by: author, at: stampNow()
      });
      if (!record) { showError(error, "Controlla i dati inseriti: nomi e nota non possono superare la lunghezza indicata."); return; }
      rememberName(author);
      if (groups.indexOf(record.who) >= 0) rememberFamily(record.who);
      busy(true);
      saveEvent(Object.assign({ t: "set" }, L.entryRecord(record))).then(done, fail);
    });

    if (remove) {
      remove.addEventListener("click", function () {
        if (!remove.classList.contains("armed")) { // a second click confirms
          remove.classList.add("armed");
          remove.textContent = "Conferma eliminazione";
          return;
        }
        busy(true);
        saveEvent({ t: "del", id: entry.id, by: me.value.trim(), at: stampNow() }).then(done, fail);
      });
    }

    openSheet(entry ? "Modifica periodo" : "Chi usa la casa", form);
    var mine = sheet;
    // Saved: close this sheet, unless it was closed meanwhile and another one is open.
    function done() { if (sheet === mine) closeSheet(); }
    refreshPreview();
    var first = entry ? submit : who.hidden ? form.querySelector(".choices input:checked, .choices input") : who;
    first.focus();
  }

  /** Everything about one day, with the ways to change it. */
  function daySheet(day, props) {
    var body = el("div", { class: "sheet-body" }, props.map(function (prop) {
      var words = describe(prop, day), info = L.dayInfo(prop, day);
      var touching = L.entriesOf(state.entries, prop.id).filter(function (entry) {
        return entry.start <= day && day <= entry.end;
      });
      // Also on a night another family already has: the house can be shared.
      var canAdd = state.store && (info.state === "free" || info.state === "closed");
      return el("div", { class: "sheet-house p" + prop.index },
        el("div", { class: "house-head" }, el("b", { class: "tag", text: prop.badge }), el("h3", { text: prop.label })),
        el("p", { class: "now" }, icon(info.state),
          el("span", null, el("strong", { text: words.status }), words.detail ? " · " + words.detail : "")),
        touching.length ? el("ul", { class: "entries" }, touching.map(function (entry) { return entryItem(entry, prop); })) : null,
        canAdd ? el("button", {
          class: "btn", type: "button",
          text: info.state === "closed" ? "Segna chi la usa" : "Segna un periodo da questo giorno",
          onclick: function () { entrySheet(null, L.draftPeriod(prop, day, state.entries)); }
        }) : null);
    }));
    openSheet(F.long.format(dateOf(day)), body);
  }

  function syncLine() {
    if (!state.store) return el("p", { class: "hint", text: "Le modifiche non sono attive su questa pagina." });
    if (state.store.kind === "local") {
      return el("p", { class: "hint", text: "Versione dimostrativa: le modifiche restano su questo dispositivo." });
    }
    if (state.sync.phase === "error") {
      return el("p", { class: "now" }, icon("warning"), el("span", null,
        el("strong", { text: "Modifiche recenti non lette." }), " " + STORE_MESSAGES[state.sync.code] + " ",
        el("button", { class: "btn quiet", type: "button", "data-key": "sync-retry", text: "Riprova", onclick: refreshEntries })));
    }
    return el("p", { class: "hint", text: state.sync.phase === "ready"
      ? "Aggiornato con le modifiche di tutti." : "Lettura delle modifiche…" });
  }

  /** Who uses each house: the list everyone can add to and change. */
  function familySection() {
    var props = visible(); // also a house whose portal could not be read: its notes are still there
    if (!props.length) return null;
    var groups = props.map(function (prop) {
      return { prop: prop, entries: L.entriesOf(state.entries, prop.id).filter(function (entry) { return entry.end > state.today; }) };
    });
    var any = groups.some(function (group) { return group.entries.length; });
    if (!state.store && !any) return null; // a read-only page with nothing to show
    var first = readable()[0] || props[0];
    return el("section", { class: "card people" },
      el("div", { class: "section-head" }, el("h2", { text: "Chi usa la casa" }),
        state.store ? el("button", {
          class: "btn primary", type: "button", "data-key": "family-add", text: "Aggiungi un periodo",
          onclick: function () {
            entrySheet(null, L.draftPeriod(first, L.firstOpenDay(first, state.today, state.entries), state.entries));
          }
        }) : null),
      el("p", { class: "hint", text: "Segna quando usi una casa, così lo sanno tutti. È un promemoria: per impedire le prenotazioni degli ospiti le date vanno fatte bloccare al gestore, e ogni periodo indica se lo sono già." }),
      syncLine(),
      el("div", { class: "stays" }, groups.map(function (group) {
        return el("div", { class: "p" + group.prop.index },
          el("h3", null, el("i", { class: "swatch" }), group.prop.label),
          group.entries.length
            ? el("ul", { class: "entries" }, group.entries.map(function (entry) { return entryItem(entry, group.prop); }))
            : el("p", { class: "empty", text: "Nessun periodo inserito." }));
      })));
  }

  /**
   * For the owner: the nights family members are counting on that are not known to be
   * blocked. Blocking is the manager's job, asked for by hand; the link only lets the
   * owner look at the portal.
   */
  /**
   * The message that asks a house's manager to block the periods still open, as its owner
   * sends it to the manager's group: the words are the owner's own, the dates are whole
   * periods (the day of arrival and the day of leaving), each said once.
   */
  function blockRequest(open) {
    var periods = [];
    open.forEach(function (item) {
      var line = "* dal " + F.written.format(dateOf(item.entry.start)) + " al " + F.written.format(dateOf(item.entry.end));
      if (periods.indexOf(line) < 0) periods.push(line);
    });
    return "Buongiorno,\nvorremmo bloccare i seguenti periodi per uso famiglia:\n\n" + periods.join("\n") +
      "\n\nDateci conferma di ricezione,\nGrazie";
  }

  function closingList(props, byId) {
    var groups = props.map(function (prop) {
      var open = [];
      L.entriesOf(state.entries, prop.id).forEach(function (entry) {
        if (entry.end <= state.today) return;
        var status = L.entryStatus(entry, prop);
        if (status.state !== "conflict" && status.open.length) open.push({ entry: entry, status: status });
      });
      return { prop: prop, open: open };
    }).filter(function (group) { return group.open.length; });
    if (!groups.length) return null;
    return el("div", { class: "todo" },
      el("h3", null, icon("warning"), "Da far bloccare al gestore"),
      groups.map(function (group) {
        var url = byId[group.prop.id] && byId[group.prop.id].portal_url;
        return el("div", { class: "p" + group.prop.index },
          el("h4", null, el("i", { class: "swatch" }), group.prop.label),
          el("ul", { class: "plain" }, group.open.map(function (item) {
            return el("li", { text: runsText(item.status.open) + " · " + item.entry.who +
              (item.status.state === "unknown" ? " · da verificare: il portale non pubblica ancora queste date" : "") });
          })),
          el("div", { class: "actions" },
            // WhatsApp opens with the message written and asks which chat to send it to: the
            // page knows no number and sends nothing itself.
            el("a", {
              class: "btn primary", "data-key": "ask:" + group.prop.id, target: "_blank", rel: "noopener noreferrer",
              href: "https://wa.me/?text=" + encodeURIComponent(blockRequest(group.open)), text: "Chiedi il blocco su WhatsApp"
            }),
            typeof url === "string" && url.indexOf("https://") === 0
              ? el("a", { class: "btn", href: url, target: "_blank", rel: "noopener noreferrer", text: "Controlla sul portale di " + group.prop.label })
              : null));
      }));
  }

  /* ---------- sections ---------- */

  function header() {
    return el("header", { class: "top" },
      el("div", null,
        el("h1", { text: state.family.title || genericTitle }),
        el("p", { class: "meta", text: "Aggiornato " + stamp(Date.parse(state.family.generated_at)) + " · si aggiorna ogni notte" })),
      el("button", { class: "btn quiet", type: "button", onclick: lock }, icon("lock"), "Blocca"));
  }

  function banner(kind, iconName, title, text) {
    return el("div", { class: "banner " + kind, role: kind === "demo" ? null : "alert" },
      icon(iconName), el("p", null, el("strong", { text: title }), " " + text));
  }

  function banners() {
    var out = [];
    var generated = Date.parse(state.family.generated_at);
    var fresh = L.freshness(generated, state.props, Date.now());
    function label(id) {
      return state.props.filter(function (prop) { return prop.id === id; })[0].label;
    }
    if (state.family.demo) {
      out.push(banner("demo", "unknown", "Versione dimostrativa.", "Date e importi sono inventati."));
    }
    if (fresh.stale) {
      out.push(banner("critical", "critical", "Dati non aggiornati.",
        "L'ultimo aggiornamento riuscito è " + stamp(generated, true) +
        ": l'aggiornamento notturno non sta funzionando. Non fare affidamento su queste disponibilità."));
    }
    fresh.lagging.forEach(function (id) {
      var prop = state.props.filter(function (p) { return p.id === id; })[0];
      out.push(banner("warning", "warning", label(id) + ": dati non aggiornati.",
        "Il portale del gestore non ha risposto all'ultimo aggiornamento; i dati sono " +
        stamp(prop.fetchedAt, true) + "."));
    });
    fresh.missing.forEach(function (id) {
      out.push(banner("critical", "critical", label(id) + ": dati non disponibili.",
        "Il portale del gestore non è stato letto, quindi questa casa non compare nel calendario."));
    });
    return out;
  }

  var PNL = "p&l"; // the tab that is not a house (a house's id has no such characters)

  /** The cost sheets the profit and loss is drawn from; none where the owner keeps no such sheet. */
  function pnlSheets() { return state.owner ? L.costSheets(state.owner.costs) : []; }

  /**
   * One tab per house: everything below shows the house chosen here. Where the costs of
   * the houses are known there is one more, for the profit and loss of all of them.
   */
  function houseTabs() {
    var options = views();
    if (options.length < 2) return null;
    var withPnl = options[options.length - 1].id === PNL;
    return el("div", null, segmented("house", withPnl ? "Vista" : "Casa", options, currentView(), showView));
  }

  /** Show a house, or the profit and loss. */
  function showView(id, key) {
    state.pnl = id === PNL;
    if (!state.pnl) {
      state.house = id;
      rememberHouse(id);
    }
    render(key);
  }

  /** The views the tabs lead to, in their order: the houses, then the profit and loss if there is one. */
  function views() {
    var all = state.props.map(function (prop) {
      return { id: prop.id, label: prop.label, index: prop.index, photo: prop.photo };
    });
    if (pnlSheets().length) all.push({ id: PNL, label: "P&L" });
    return all;
  }

  function currentView() { return state.pnl ? PNL : state.house; }

  /* A finger drawn across the page turns to the next view, as it would the pages of an app:
   * to the left for the one after, to the right for the one before. Not at the ends, not while
   * a form is open, and not where the finger is on something that scrolls sideways itself. */
  var stroke = null;

  function scrollsSideways(node) {
    for (; node && node !== root && node.nodeType === 1; node = node.parentNode) {
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName)) return true;
      var flow = getComputedStyle(node).overflowX;
      if ((flow === "auto" || flow === "scroll") && node.scrollWidth > node.clientWidth + 1) return true;
    }
    return false;
  }

  root.addEventListener("touchstart", function (event) {
    stroke = null;
    if (!state || !state.props || sheet || event.touches.length !== 1 || scrollsSideways(event.target)) return;
    stroke = { x: event.touches[0].clientX, y: event.touches[0].clientY, at: Date.now() };
  }, { passive: true });

  root.addEventListener("touchend", function (event) {
    var begun = stroke, touch = event.changedTouches[0];
    stroke = null;
    if (!begun || !touch) return;
    var across = touch.clientX - begun.x, down = touch.clientY - begun.y;
    // Long, quick and level: anything else is a scroll, or a tap that slipped.
    if (Math.abs(across) < 70 || Math.abs(across) < 2.5 * Math.abs(down) || Date.now() - begun.at > 700) return;
    var all = views().map(function (view) { return view.id; });
    var next = all.indexOf(currentView()) + (across < 0 ? 1 : -1);
    if (next < 0 || next >= all.length) return;
    showView(all[next]);
    window.scrollTo(0, 0);
  }, { passive: true });

  /** How far away a check-in is, in words. */
  function inDaysLabel(days) {
    return days === 0 ? "oggi" : days === 1 ? "domani" : "tra " + F.int.format(days) + " giorni";
  }

  /** One house: who is there and who is coming next, and nothing else. */
  function houseCard(prop) {
    // Everything about the house goes in `body`; beside it (above it on a phone) its photo,
    // so that which house this is can be seen before anything is read.
    var body = el("div", { class: "house-body" },
      el("div", { class: "house-head" }, el("i", { class: "swatch" }), el("h3", { text: prop.label }),
        prop.unit ? el("span", { class: "unit", text: prop.unit }) : null));
    var card = el("article", { class: "card house p" + prop.index + (prop.photo ? " with-photo" : "") },
      // Decoded before it is painted: the page is redrawn whole at every change, and a
      // picture that came a frame late would blink each time.
      prop.photo ? el("div", { class: "house-photo" },
        el("img", { src: prop.photo, alt: "Foto di " + prop.label, decoding: "sync" })) : null,
      body);
    if (prop.missing) {
      append(body, el("p", { class: "now" }, icon("unknown"),
        el("span", null, el("strong", { text: "Dati non disponibili" }), " · il portale del gestore non è stato letto")));
      return card;
    }
    append(body, el("h4", { text: "Prossimi ospiti" }));
    var guests = L.upcomingGuests(prop, state.today);
    if (!guests.length) {
      append(body, el("p", { class: "empty", text: "Nessun ospite in arrivo fino a " + shortLabel(prop.to - 1) + "." }));
      return card;
    }
    var expanded = !!state.allGuests[prop.id];
    var shown = expanded ? guests : guests.slice(0, GUESTS_SHOWN);
    // What the portal says about each stay, where it says anything: the channel, the number of guests.
    var withDetails = guests.some(function (guest) { return !!stayDetails(guest.stay); });
    var withChannel = guests.some(function (guest) { return !!guest.stay.channel; });
    var next = guests.filter(function (guest) { return !guest.current; })[0];
    // In a box that scrolls on its own: whatever the screen and the device's font, a
    // table too wide for the card must not make the whole page scroll sideways.
    append(body, el("div", { class: "scroll" }, el("table", { class: "rows" },
      el("thead", null, el("tr", null,
        el("th", { scope: "col" }, "Check-in", el("span", { class: "sub", text: "dalle " + prop.hours.checkIn })),
        el("th", { scope: "col" }, "Check-out", el("span", { class: "sub", text: "entro le " + prop.hours.checkOut })),
        el("th", { scope: "col", class: "num", text: "Notti" }),
        withDetails ? el("th", { scope: "col", text: withChannel ? "Canale" : "Ospiti" }) : null)),
      el("tbody", null, shown.map(function (guest) {
        var stay = guest.stay;
        var known = stay.guests !== null && stay.guests !== undefined;
        // The soonest arrival also says how far away it is; guests already there say so.
        var when = guest.current ? "in casa ora" : guest === next ? inDaysLabel(guest.inDays) : null;
        return el("tr", { class: guest.current ? "current" : null },
          // The year once, on the day the stay ends: twice, the row is too wide for a phone.
          el("td", { class: "date" }, startLabel(stay), when ? el("span", { class: "sub", text: when }) : null),
          el("td", { class: "date", text: dayLabel(stay.end) }),
          el("td", { class: "num", text: F.int.format(stay.end - stay.start) }),
          withDetails ? el("td", null, stay.channel || (known ? guestsLabel(stay.guests) : ""),
            stay.channel && known ? el("span", { class: "sub", text: guestsLabel(stay.guests) }) : null) : null);
      })))));
    if (guests.length > GUESTS_SHOWN) {
      var key = "guests:" + prop.id;
      append(body, el("button", {
        class: "btn quiet more", type: "button", "data-key": key, "aria-expanded": String(expanded),
        text: expanded ? "Mostra meno" : "Mostra tutti i " + guests.length + " soggiorni",
        onclick: function () { state.allGuests[prop.id] = !expanded; render(key); }
      }));
    }
    return card;
  }

  function availability() {
    return el("section", { class: "houses", "aria-label": "Prossimi ospiti" }, visible().map(houseCard));
  }

  function seriesLegend(props) {
    return el("div", { class: "legend" }, props.map(function (prop) {
      return el("span", { class: "p" + prop.index }, el("i", { class: "swatch" }), prop.label);
    }));
  }

  var GAP = 4; // px between day tiles; the stylesheet's --gap must say the same

  /**
   * A horizontal position in a row of `columns` day tiles, as the inside of a calc().
   * Tiles are separated by gaps, so the position is whole steps (a tile plus a gap)
   * plus a share of one tile.
   */
  function edge(position, isRightEdge, columns) {
    var at = L.cellPosition(position, isRightEdge);
    return at.cell + " * (100% + " + GAP + "px) / " + columns + " + " +
      at.part.toFixed(5) + " * (100% - " + ((columns - 1) * GAP) + "px) / " + columns;
  }
  function place(node, left, right, columns) {
    node.style.left = "calc(" + edge(left, false, columns) + ")";
    node.style.width = "calc((" + edge(right, true, columns) + ") - (" + edge(left, false, columns) + "))";
  }

  /** Legend of the calendar: who is who, what a bar means, and at what hour it starts and ends. */
  function calendarKey(props) {
    var first = props[0].hours;
    var sameHours = props.every(function (prop) {
      return prop.hours.checkIn === first.checkIn && prop.hours.checkOut === first.checkOut;
    });
    // A three-day stay drawn to scale: in at check-in time, out at check-out time.
    var sample = el("i", { class: "bar booking" });
    place(sample, first.inAt, 2 + first.outAt, 3);
    var arrives = el("span", { class: "cap", text: first.checkIn });
    arrives.style.left = "calc(" + edge(first.inAt, false, 3) + ")";
    var leaves = el("span", { class: "cap", text: first.checkOut });
    leaves.style.left = "calc(" + edge(2 + first.outAt, true, 3) + ")";
    var art = el("div", { class: "keyart p" + props[0].index, "aria-hidden": "true" },
      el("i", { class: "keytile" }), el("i", { class: "keytile" }), el("i", { class: "keytile" }), sample, arrives, leaves);

    var words = sameHours
      ? [el("strong", { text: "Check-in dalle " + first.checkIn + ", check-out entro le " + first.checkOut + "." }),
         " Ogni barra inizia e finisce a quell'ora: nei cambi in giornata restano scoperte le ore in mezzo."]
      : [el("strong", { text: "Ogni barra inizia all'ora di check-in e finisce all'ora di check-out." }), " " +
         props.map(function (prop) {
           return prop.label + ": dalle " + prop.hours.checkIn + ", entro le " + prop.hours.checkOut;
         }).join("; ") + "."];

    return el("div", { class: "key" },
      el("div", { class: "legend" },
        props.map(function (prop) {
          return el("span", { class: "p" + prop.index }, el("b", { class: "tag", text: prop.badge }), prop.label);
        }),
        el("span", null, el("i", { class: "chip solid" }), "Ospiti"),
        el("span", null, el("i", { class: "chip pale" }), "Bloccata (chiusa alla vendita, senza ospiti)"),
        el("span", null, el("i", { class: "chip mid" }), "Uso famiglia"),
        el("span", null, el("i", { class: "chip dashed" }), "Uso famiglia non ancora bloccato"),
        el("span", null, el("i", { class: "chip none" }), "Libera")),
      el("div", { class: "hours" }, art, el("p", null, words)));
  }

  /** Who in the family has the house on the night of `day`, in words; "" if nobody. */
  function familyWords(prop, day) {
    return L.entriesOf(state.entries, prop.id).filter(function (entry) {
      return entry.start <= day && day < entry.end;
    }).map(function (entry) {
      var kind = L.entryStatus(entry, prop).state;
      return "Uso famiglia: " + entry.who + (kind === "pending" ? " (da far bloccare al gestore)"
        : kind === "conflict" ? " (ci sono ospiti)" : kind === "unknown" ? " (non verificabile sul portale)" : "");
    }).join("; ");
  }

  function dayTip(day, props) {
    return [el("div", { class: "tip-title", text: F.long.format(dateOf(day)) })].concat(props.map(function (prop) {
      var words = describe(prop, day), family = familyWords(prop, day);
      return el("div", { class: "tip-row p" + prop.index },
        el("i", { class: "dash" }),
        el("span", null, el("strong", { text: words.status }), el("span", { class: "who", text: " · " + prop.label })),
        words.detail ? el("span", { class: "detail", text: words.detail }) : null,
        family ? el("span", { class: "detail fam", text: family }) : null);
    }));
  }

  /** What may be written inside a stay's bar, longest first; the first that fits is used. */
  function barLabels(stay) {
    if (stay.kind === "closed") return ["Bloccata"];
    var guests = stay.guests === null || stay.guests === undefined ? "" : guestsLabel(stay.guests);
    if (guests && stay.channel) return [guests + " · " + stay.channel, guests];
    return guests ? [guests] : (stay.channel ? [stay.channel] : []);
  }

  function monthBlock(month, props, focusDay, runs) {
    var first = L.dayOf(month.y, month.m, 1);
    var block = el("section", { class: "month" + (first + L.daysInMonth(month.y, month.m) <= state.today ? " past" : "") },
      el("h3", { text: F.month.format(dateOf(first)) }),
      el("div", { class: "dow", "aria-hidden": "true" }, DOW.map(function (letter) { return el("span", { text: letter }); })));
    L.monthWeeks(month.y, month.m).forEach(function (week) {
      var cells = el("div", { class: "cells" }, week.map(function (day, column) {
        if (day === null) return el("span", { class: "pad" });
        var spoken = F.long.format(dateOf(day)) + ". " + props.map(function (prop) {
          var words = describe(prop, day), family = familyWords(prop, day);
          return prop.label + ": " + words.status + (words.detail ? ", " + words.detail : "") + (family ? ". " + family : "");
        }).join(". ") + ".";
        return el("button", {
          type: "button", "data-day": String(day), "data-key": "day:" + day,
          tabindex: day === focusDay ? "0" : "-1", "aria-label": spoken,
          class: "day" + (column >= 5 ? " we" : "") + (day < state.today ? " past" : "") + (day === state.today ? " today" : "")
        }, el("span", { class: "n", text: String(L.parts(day).d) }));
      }));
      var lanes = el("div", { class: "lanes", "aria-hidden": "true" }, props.map(function (prop) {
        var segments = L.weekSegments(prop.stays, week, prop.hours.inAt, prop.hours.outAt);
        return el("div", { class: "lane p" + prop.index }, segments.map(function (segment) {
          var stay = segment.stay;
          var bar = el("div", {
            class: "bar " + (segment.kind === "closed" ? "closed" : "booking") + (segment.cutLeft ? " cut-l" : "") +
              (segment.cutRight ? " cut-r" : "") + (stay.end <= state.today ? " past" : ""),
            "data-stay": prop.index + ":" + stay.start, "data-from": String(stay.start), "data-to": String(stay.end)
          }, el("b", { class: "tag", text: prop.badge }), el("span", { class: "txt" }));
          bar.labels = barLabels(stay);
          place(bar, segment.left, segment.right, 7);
          return bar;
        }), familyBars(prop, week, runs[prop.id]));
      }));
      block.appendChild(el("div", { class: "week" }, cells, lanes));
    });
    return block;
  }

  /**
   * The family's periods of one house in one week row; they sit over the bars beneath.
   * `runs` are the stretches worked out by L.familyRuns: where two periods share
   * nights there is one bar with both names, flush against the bars on either side.
   */
  function familyBars(prop, week, runs) {
    return L.clipToWeek(runs, week).map(function (segment) {
      var run = segment.span, names = [];
      run.entries.forEach(function (entry) { if (names.indexOf(entry.who) < 0) names.push(entry.who); });
      var full = names.join(" + "), short = names.map(L.shortName).join(" + ");
      var suffix = run.state === "pending" ? " · da far bloccare" : run.state === "conflict" ? " · ci sono ospiti" : "";
      var over = run.entries.every(function (entry) { return entry.end <= state.today; });
      var bar = el("div", {
        class: "bar family " + run.state +
          (segment.cutLeft ? " cut-l" : run.joinLeft ? " join-l" : "") +
          (segment.cutRight ? " cut-r" : run.joinRight ? " join-r" : "") + (over ? " past" : ""),
        "data-stay": "family:" + run.entries.map(function (entry) { return entry.id; }).join("+"),
        "data-from": String(Math.floor(run.from + 1e-9)), "data-to": String(Math.floor(run.to + 1e-9)),
        title: full + suffix // a piece too short for words still says whose it is
      }, el("b", { class: "tag", text: prop.badge }), el("span", { class: "txt" }));
      // Longest first; the first that fits is written (see fitLabels).
      bar.labels = [full + suffix, full, short + suffix, short].filter(function (label, index, all) {
        return all.indexOf(label) === index;
      });
      place(bar, segment.left, segment.right, 7);
      return bar;
    });
  }

  /**
   * Write a label inside each stay's bar, but only where it really fits: text is
   * measured against the bar, never clipped. A stay drawn over several rows gets its
   * label once, on the first piece wide enough; a piece too short even for the
   * house's initial shows the bare bar. A family period is drawn over the manager's
   * bars, so those are labelled only in the part left in view before it.
   */
  function fitLabels() {
    var placed = {};
    var lanes = root.querySelectorAll(".months .lane");
    for (var l = 0; l < lanes.length; l++) {
      var bars = lanes[l].children, covers = [];
      for (var c = 0; c < bars.length; c++) {
        if (bars[c].classList.contains("family")) covers.push(bars[c].getBoundingClientRect());
      }
      for (var i = 0; i < bars.length; i++) fitLabel(bars[i], covers, placed);
    }
  }

  function fitLabel(bar, covers, placed) {
    var tag = bar.firstChild, text = bar.lastChild;
    text.textContent = "";
    tag.hidden = false;
    var room = bar.getBoundingClientRect();
    var limit = room.right, startCovered = false;
    if (!bar.classList.contains("family")) {
      covers.forEach(function (cover) {
        if (cover.right <= room.left || cover.left >= room.right) return;
        if (cover.left <= room.left + 1) startCovered = true;
        else limit = Math.min(limit, cover.left);
      });
    }
    if (startCovered || tag.getBoundingClientRect().right > limit - 2) { tag.hidden = true; return; }
    var stayKey = bar.getAttribute("data-stay");
    if (placed[stayKey]) return;
    // Up to its own end a bar keeps its padding; up to a period drawn over it, a small gap.
    var padding = limit < room.right ? 3 : (parseFloat(getComputedStyle(bar).paddingRight) || 0);
    var labels = bar.labels || [];
    for (var j = 0; j < labels.length; j++) {
      text.textContent = labels[j];
      if (text.getBoundingClientRect().right <= limit - padding) { placed[stayKey] = true; break; }
      text.textContent = "";
    }
  }

  function calendar() {
    var props = readable();
    var section = el("section", { class: "card calendar" });
    var key = "past-months";
    append(section, el("div", { class: "section-head" },
      el("h2", { text: "Calendario" }),
      el("button", {
        class: "btn quiet", type: "button", "data-key": key, "aria-pressed": String(state.showPast),
        text: state.showPast ? "Nascondi i mesi passati" : "Mostra i mesi passati",
        onclick: function () { state.showPast = !state.showPast; render(key); }
      })));
    if (!props.length) {
      append(section, el("p", { class: "empty", text: "Nessun calendario da mostrare." }));
      return section;
    }
    append(section, calendarKey(props));
    append(section, el("p", { class: "hint tap", text: state.store
      ? "Tocca un giorno per i dettagli o per segnare chi usa la casa."
      : "Tocca un giorno per i dettagli." }));
    var today = L.parts(state.today);
    var to = Math.max.apply(null, props.map(function (prop) { return prop.to; }));
    var from = state.showPast ? Math.min.apply(null, props.map(function (prop) { return prop.from; }))
      : L.dayOf(today.y, today.m, 1);
    var months = L.monthsBetween(from, to);
    var focusDay = state.today >= from && state.today < to ? state.today : from;
    // The family's periods of each house, as stretches to draw: worked out once for all the months.
    var runs = {};
    props.forEach(function (prop) { runs[prop.id] = L.familyRuns(L.entriesOf(state.entries, prop.id), prop); });
    var grid = el("div", { class: "months" + (props.length === 1 ? " single" : "") },
      months.map(function (month) { return monthBlock(month, props, focusDay, runs); }));
    var laneHeight = props.length === 1 ? 26 : 20;
    grid.style.setProperty("--lane-h", laneHeight + "px");
    grid.style.setProperty("--lanes-h", (props.length * laneHeight + (props.length - 1) * GAP) + "px");

    attachTips(grid, ".day", function (node) { return dayTip(Number(node.getAttribute("data-day")), props); }, false);
    grid.addEventListener("click", function (event) {
      var node = event.target.closest ? event.target.closest(".day") : null;
      if (node) daySheet(Number(node.getAttribute("data-day")), props);
    });
    grid.addEventListener("keydown", function (event) {
      var step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[event.key];
      var current = event.target.closest ? event.target.closest(".day") : null;
      if (!step || !current) return;
      var next = grid.querySelector('.day[data-day="' + (Number(current.getAttribute("data-day")) + step) + '"]');
      if (!next) return;
      event.preventDefault();
      current.tabIndex = -1;
      next.tabIndex = 0;
      next.focus();
    });
    append(section, grid);
    return section;
  }

  /** Stays as a table. Rows are grouped by check-in year so that dates stay short on a phone. */
  function stayRows(stays, prop) {
    var year = L.parts(state.today).y;
    var rows = [];
    stays.forEach(function (stay) {
      var stayYear = L.parts(stay.start).y;
      if (stayYear !== year) {
        year = stayYear;
        rows.push(el("tr", { class: "year" }, el("th", { colspan: "4", scope: "colgroup", text: String(year) })));
      }
      var running = stay.start <= state.today && state.today < stay.end;
      rows.push(el("tr", { class: running ? "current" : null },
        el("td", { class: "date", text: F.day.format(dateOf(stay.start)) }),
        el("td", { class: "date", text: F.day.format(dateOf(stay.end)) }),
        el("td", { class: "num", text: F.int.format(stay.end - stay.start) }),
        el("td", null, el("span", { class: "state" }, icon(stay.kind),
          el("span", null, STATE_TEXT[stay.kind] + (running ? " · in corso" : ""),
            stayDetails(stay) ? el("span", { class: "sub", text: stayDetails(stay) }) : null)))));
    });
    return el("div", { class: "scroll" }, el("table", { class: "rows" },
      el("thead", null, el("tr", null,
        el("th", { scope: "col" }, "Check-in", el("span", { class: "sub", text: "dalle " + prop.hours.checkIn })),
        el("th", { scope: "col" }, "Check-out", el("span", { class: "sub", text: "entro le " + prop.hours.checkOut })),
        el("th", { scope: "col", class: "num", text: "Notti" }), el("th", { scope: "col", text: "Stato" }))),
      el("tbody", null, rows)));
  }

  function staysSection() {
    var props = readable();
    if (!props.length) return null;
    return el("section", { class: "card" },
      el("div", { class: "section-head" }, el("h2", { text: "Soggiorni e blocchi" }),
        el("span", { class: "hint", text: "Gli stessi dati del calendario, in tabella." })),
      el("div", { class: "stays" }, props.map(function (prop) {
        var upcoming = prop.stays.filter(function (stay) { return stay.end > state.today; });
        var past = prop.stays.filter(function (stay) { return stay.end <= state.today; }).reverse();
        var open = !!state.pastStays[prop.id];
        var key = "past-stays:" + prop.id;
        return el("div", { class: "p" + prop.index },
          el("h3", null, el("i", { class: "swatch" }), prop.label),
          upcoming.length ? stayRows(upcoming, prop) : el("p", { class: "empty", text: "Nessun soggiorno in programma." }),
          past.length ? el("button", {
            class: "btn quiet more", type: "button", "data-key": key, "aria-expanded": String(open),
            text: open ? "Nascondi i soggiorni passati" : "Mostra i soggiorni passati (" + past.length + ")",
            onclick: function () { state.pastStays[prop.id] = !open; render(key); }
          }) : null,
          open && past.length ? stayRows(past, prop) : null);
      })));
  }

  /* ---------- owner area ---------- */

  function tile(label, value, note) {
    return el("div", { class: "tile" }, el("div", { class: "label", text: label }),
      el("div", { class: "value", text: value }), note ? el("div", { class: "note", text: note }) : null);
  }

  function netChart(year, series) {
    var peak = 0;
    series.forEach(function (s) {
      s.rows.forEach(function (row) { if (row.net_cents > peak) peak = row.net_cents; });
    });
    if (peak <= 0) return null;
    var scale = L.axisScale(peak);
    var yaxis = el("div", { class: "yaxis", "aria-hidden": "true" }, scale.ticks.map(function (tick) {
      var mark = el("span", { text: F.money0.format(tick / 100) });
      mark.style.bottom = (tick / scale.max * 100) + "%";
      return mark;
    }));
    var area = el("div", { class: "area" }, scale.ticks.slice(1).map(function (tick) {
      var line = el("i", { class: "gridline" });
      line.style.bottom = (tick / scale.max * 100) + "%";
      return line;
    }));
    var groups = el("div", { class: "groups" });
    for (var m = 1; m <= 12; m++) {
      (function (month) {
        var monthName = F.monthName.format(dateOf(L.dayOf(year, month, 1)));
        var spoken = monthName + " " + year + ". " + series.map(function (s) {
          return s.prop.label + ": " + euro(s.rows[month - 1].net_cents);
        }).join(". ") + ".";
        var group = el("button", { class: "group", type: "button", "data-month": String(month), "aria-label": spoken },
          series.map(function (s) {
            var value = s.rows[month - 1].net_cents;
            if (!(value > 0)) return null;
            var column = el("i", { class: "col p" + s.prop.index });
            column.style.height = (value / scale.max * 100) + "%";
            return column;
          }));
        groups.appendChild(group);
      })(m);
    }
    area.appendChild(groups);
    attachTips(groups, ".group", function (node) {
      var month = Number(node.getAttribute("data-month"));
      return [el("div", { class: "tip-title", text: F.month.format(dateOf(L.dayOf(year, month, 1))) })].concat(
        series.map(function (s) {
          return el("div", { class: "tip-row p" + s.prop.index }, el("i", { class: "dash" }),
            el("span", null, el("span", { class: "val", text: euro(s.rows[month - 1].net_cents) }),
              el("span", { class: "who", text: " · " + s.prop.label })));
        }));
    });
    var months = [];
    for (var n = 1; n <= 12; n++) months.push(el("span", { text: F.monthShort.format(dateOf(L.dayOf(year, n, 1))).replace(".", "") }));
    return el("figure", { class: "chart" },
      el("figcaption", { text: "Netto proprietario per mese · " + year }),
      series.length > 1 ? seriesLegend(series.map(function (s) { return s.prop; })) : null,
      el("div", { class: "plot" }, yaxis, area, el("div", { class: "xaxis", "aria-hidden": "true" }, months)));
  }

  function headerRow(columns) {
    return el("tr", null, columns.map(function (column) {
      return el("th", { scope: "col", class: column.numeric ? "num" : null, text: column.name });
    }));
  }
  function bodyRow(columns, values, className) {
    return el("tr", { class: className || null }, values.map(function (value, i) {
      return el("td", { class: columns[i].numeric ? "num" : (columns[i].date ? "date" : null), text: value });
    }));
  }
  /**
   * A table with many columns: as narrow as its figures allow (headings wrap), and where
   * a phone is narrower still it scrolls sideways inside its own box, first column in view.
   */
  function wideTable(columns, body, footer) {
    var table = el("table", { class: "rows kpi" },
      el("thead", null, headerRow(columns)),
      body.length ? el("tbody", null, body) : null,
      footer ? el("tfoot", null, footer) : null);
    return el("div", { class: "scroll" }, table);
  }

  /** Monthly statistics of one house, with the columns its manager's portal publishes. */
  function kpiTable(year, entry) {
    var names = entry.owner.kpi_columns || [];
    var adrAt = L.roleIndex(entry.owner.kpi_roles, "adr"), netAt = L.roleIndex(entry.owner.kpi_roles, "net");
    var withGross = adrAt !== null;            // gross can be rebuilt as nights x ADR...
    var withRatio = withGross && netAt !== null; // ...and compared with the owner's net
    var columns = [{ name: "Mese" }, { name: "Notti", numeric: true }, { name: "Occ.", numeric: true }];
    names.forEach(function (name, i) {
      columns.push({ name: name, numeric: true });
      if (i === adrAt) columns.push({ name: "Lordo*", numeric: true });
    });
    // The hard space keeps the heading to two lines over a column of narrow percentages.
    if (withRatio) columns.push({ name: "Netto\u00a0su lordo*", numeric: true });

    // Whole euros, and the unit said once under the table: twelve months of several
    // amounts each have to be read on a phone with as little sideways scrolling as can be.
    function line(label, nightCount, occupancy, vals, gross, net) {
      var out = [label, F.int.format(nightCount), pct(occupancy)];
      vals.forEach(function (value, i) {
        out.push(whole(value));
        if (i === adrAt) out.push(whole(gross));
      });
      if (withRatio) out.push(pct(gross && net !== null ? net / gross : null));
      return out;
    }
    var total = entry.house;
    return [
      el("h4", { text: "Statistiche mensili" }),
      wideTable(columns,
        entry.rows.map(function (row) {
          return bodyRow(columns, line(capital(F.monthName.format(dateOf(L.dayOf(year, row.m, 1)))), row.nights,
            row.occupancy, row.vals, row.gross_cents, row.net_cents), row.nights ? null : "zero");
        }),
        bodyRow(columns, line("Totale " + year, total.nights, total.occupancy, total.vals, total.gross_cents, total.net_cents))),
      el("p", { class: "hint", text: "Importi in euro, arrotondati all'unità: il totale può differire di poco dalla somma dei mesi." +
        (withGross ? " * Calcolato: lordo = notti × ADR." : "") })
    ];
  }

  /** The figures of the house on show, and the nights still to be blocked for the family. */
  function ownerSection() {
    if (!state.owner) return null; // the figures did not open: the calendar is shown without them
    var section = el("section", { class: "card" });
    var byId = {};
    state.owner.properties.forEach(function (raw) { byId[raw.id] = raw; });
    var scoped = readable().filter(function (prop) { return byId[prop.id] && !byId[prop.id].missing; });
    var years = L.kpiYears(scoped.map(function (prop) { return byId[prop.id]; }));
    var current = L.parts(state.today).y;
    if (years.indexOf(state.year) < 0) state.year = years.indexOf(current) >= 0 ? current : years[years.length - 1];
    var year = state.year;

    append(section, el("div", { class: "section-head" },
      el("h2", { text: "Area proprietà" }),
      years.length > 1 ? segmented("year", "Anno", years.map(function (y) { return { id: y, label: String(y) }; }), year,
        function (id, key) { state.year = id; render(key); }) : null));
    append(section, closingList(scoped, byId));
    if (!years.length) {
      append(section, el("p", { class: "empty", text: "Nessun dato economico disponibile per questa casa." }));
      return section;
    }
    var series = scoped.map(function (prop) {
      var house = L.houseYear(byId[prop.id], year);
      return { prop: prop, owner: byId[prop.id], house: house, rows: house.rows };
    });
    var total = L.combine(series.map(function (entry) { return entry.house; }), year);
    var scope = series.length > 1 ? "Totale di " + series.length + " case" : series[0].prop.label;
    append(section, [
      el("p", { class: "hint", text: scope + " · anno " + year + " · valori riportati dai portali dei gestori" }),
      el("div", { class: "tiles" },
        tile("Netto proprietario", euro(total.net_cents)),
        tile("Ricavo lordo", euro(total.gross_cents), "notti × ADR"),
        tile("Netto su lordo", pct(total.net_ratio)),
        tile("Notti vendute", F.int.format(total.nights)),
        tile("Occupancy", pct(total.occupancy), "sui " + L.daysInYear(year) + " giorni dell'anno"),
        tile("ADR medio", euro(total.adr_cents), "ponderato sulle notti")),
      netChart(year, series),
      el("div", { class: "kpi-tables" }, series.map(function (entry) {
        return el("div", { class: "p" + entry.prop.index },
          el("h3", null, el("i", { class: "swatch" }), entry.prop.label),
          kpiTable(year, entry));
      }))
    ]);
    return section;
  }

  /* ---------- profit and loss ---------- */

  function missing(value) { return value === null || value === undefined; }
  function amount(cents) { return missing(cents) ? "–" : F.amount.format(cents / 100); }
  function pct1(ratio) { return missing(ratio) ? "–" : F.pct1.format(ratio); }
  function nightsCount(count) { return missing(count) ? "–" : F.int.format(count); }
  /**
   * A year of a house is read in euros, not in cents: here amounts are rounded to the
   * euro (halves away from zero, and never "-0"). The cents stay in the houses' own
   * tables and in the tooltips of the chart.
   */
  function wholeEuros(cents) {
    var value = Math.round(Math.abs(cents) / 100);
    return cents < 0 && value ? -value : value;
  }
  function whole(cents) { return missing(cents) ? "–" : F.int.format(wholeEuros(cents)); }
  function euro0(cents) { return missing(cents) ? "–" : F.money0.format(wholeEuros(cents)); }

  /**
   * The statement: income, costs and result, a column per house and one for all of them.
   * Amounts are plain numbers in whole euros; the unit is said once, above.
   */
  function pnlTable(props, result) {
    var columns = result.houses.concat(props.length > 1 ? [result.total] : []);
    var width = columns.length + 1;
    /** One part of the statement: its own group of rows, under a heading that names it. */
    function part(text, rows) {
      return el("tbody", null,
        el("tr", { class: "part" }, el("th", { colspan: String(width), scope: "rowgroup", text: text })), rows);
    }
    function row(label, sub, cells, strong) {
      return el("tr", { class: strong ? "sum" : null },
        el("th", { scope: "row" }, label, sub ? el("span", { class: "sub", text: sub }) : null),
        cells.map(function (cell) {
          return el("td", { class: "num" }, cell.text, cell.sub ? el("span", { class: "sub", text: cell.sub }) : null);
        }));
    }
    function each(format, field) {
      return columns.map(function (column) { return { text: format(column[field]) }; });
    }
    var income = [
      row("ADR medio", "per notte", each(amount, "adr_cents")), // a price: to the cent
      row("Notti vendute", null, each(nightsCount, "nights")),
      row("Ricavi lordi", "notti × ADR", each(whole, "gross_cents")),
      // What stays with the manager, the channels and the tax office, and its share of the gross.
      row("Trattenute", "Tasse + Gestione Agenzia", columns.map(function (column) {
        return { text: whole(column.withheld_cents), sub: column.withheld_ratio === null ? null : pct(column.withheld_ratio) };
      })),
      row("Ricavi netti", "netto proprietario", each(whole, "net_cents"), true)
    ];
    var costs = result.lines.map(function (line) {
      var cells = line.amounts.map(function (value) { return { text: value === null ? "n.a." : whole(value) }; });
      if (props.length > 1) cells.push({ text: whole(line.total) });
      return row(line.label, null, cells);
    });
    costs.push(row("Totale spese", null, each(whole, "costs_cents"), true));
    var outcome = [
      row("Margine", "ricavi netti − spese", each(whole, "margin_cents"), true),
      row("Margine %", "sul ricavo netto", each(pct1, "margin_ratio")),
      // At this year's net per night, how many nights pay the year's costs.
      row("Notti di pareggio", "per coprire le spese", each(nightsCount, "breakeven_nights"))
    ];
    var table = el("table", { class: "rows kpi statement" },
      el("thead", null, el("tr", null,
        el("th", { scope: "col", text: "Voce" }),
        props.map(function (prop) {
          // A long name wraps under its first word, which stays beside the swatch.
          var cut = prop.label.indexOf(" ");
          return el("th", { scope: "col", class: "num p" + prop.index },
            el("span", { class: "keep" }, el("i", { class: "swatch" }), cut < 0 ? prop.label : prop.label.slice(0, cut)),
            cut < 0 ? null : prop.label.slice(cut));
        }),
        props.length > 1 ? el("th", { scope: "col", class: "num", text: "Totale" }) : null)),
      part("Entrate", income), part("Spese", costs), part("Risultato", outcome));
    return el("div", { class: "scroll" }, table);
  }

  /**
   * Where the costs go: one bar per cost, the largest first, split by house. The bars
   * rank the costs; the figures are beside them and, house by house, in the statement.
   */
  function costBars(props, result, year) {
    var lines = result.lines.filter(function (line) { return line.total > 0; })
      .sort(function (a, b) { return b.total - a.total; });
    if (!lines.length) return null;
    var most = lines[0].total, all = result.total.costs_cents;
    var list = el("div", { class: "costs" }, lines.map(function (line, index) {
      var share = all > 0 ? line.total / all : null;
      var spoken = line.label + ": " + euro(line.total) + (share === null ? "" : ", " + pct(share) + " delle spese") + ". " +
        props.map(function (prop, i) {
          return prop.label + ": " + (line.amounts[i] === null ? "non applicabile" : euro(line.amounts[i]));
        }).join(". ") + "." + (line.note ? " Nota: " + line.note + "." : "");
      var track = el("span", { class: "track", "aria-hidden": "true" }, props.map(function (prop, i) {
        var value = line.amounts[i];
        if (!(value > 0)) return null;
        var segment = el("i", { class: "seg-bar p" + prop.index });
        segment.style.width = (value / most * 100) + "%";
        return segment;
      }));
      return el("button", { class: "cost", type: "button", "data-line": String(index), "aria-label": spoken },
        el("span", { class: "cost-name" }, line.label, line.note ? el("span", { class: "sub", text: line.note }) : null),
        el("span", { class: "cost-value" }, euro0(line.total),
          share === null ? null : el("span", { class: "sub", text: pct(share) })),
        track);
    }));
    attachTips(list, ".cost", function (node) {
      var line = lines[Number(node.getAttribute("data-line"))];
      return [el("div", { class: "tip-title", text: line.label })].concat(props.map(function (prop, i) {
        return el("div", { class: "tip-row p" + prop.index }, el("i", { class: "dash" }),
          el("span", null, el("span", { class: "val", text: line.amounts[i] === null ? "n.a." : euro(line.amounts[i]) }),
            el("span", { class: "who", text: " · " + prop.label })));
      }), line.note ? el("div", { class: "tip-note", text: line.note }) : null);
    });
    return el("figure", { class: "chart" },
      el("figcaption", { text: "Dove vanno le spese · " + year }),
      props.length > 1 ? seriesLegend(props) : null,
      list);
  }

  /** "2026", "2026 e 2027", "2026, 2027 e 2028". */
  /** The year's profit and loss of the houses: what came in, what they cost, what is left. */
  function pnlSection() {
    var byId = {};
    state.owner.properties.forEach(function (raw) { byId[raw.id] = raw; });
    var props = state.props.filter(function (prop) { return !prop.missing && byId[prop.id] && !byId[prop.id].missing; });
    // A year's income is known only while the portals are read for it (see coveredYears).
    var readFor = L.coveredYears(props);
    // Every year the portals are read for can be chosen: one the workbook has no sheet for
    // yet is set against the costs of the latest sheet before it (see L.sheetFor).
    var written = pnlSheets();
    var years = written.length ? readFor : [];
    var current = L.parts(state.today).y;
    if (years.indexOf(state.pnlYear) < 0) state.pnlYear = years.indexOf(current) >= 0 ? current : years[years.length - 1];
    var year = state.pnlYear;
    var sheet = years.length ? L.sheetFor(written, year) : null;
    var chooser = null;
    if (years.length > 1) {
      chooser = el("select", {
        class: "input year", id: "pnl-year", "data-key": "pnl-year", "aria-label": "Anno",
        onchange: function () { state.pnlYear = Number(chooser.value); render("pnl-year"); }
      }, years.map(function (y) {
        var option = el("option", { value: String(y), text: String(y) });
        option.selected = y === year;
        return option;
      }));
    }
    var section = el("section", { class: "card pnl" },
      el("div", { class: "section-head" }, el("h2", { text: "P&L · conto economico" }), chooser));
    if (!props.length) {
      append(section, el("p", { class: "empty", text: "Nessun dato dei portali disponibile: il conto economico non si può calcolare." }));
      return section;
    }
    if (!sheet) {
      append(section, el("p", { class: "empty",
        text: "I dati dei portali non coprono un anno intero: il conto economico non si può calcolare." }));
      return section;
    }
    var result = L.profitAndLoss(props.map(function (prop) { return byId[prop.id]; }), sheet, year);
    var total = result.total;
    var revised = "";
    if (typeof sheet.updated === "string" && /^\d{4}-\d{2}-\d{2}$/.test(sheet.updated)) {
      revised = " (ultima revisione: " + F.shortYear.format(dateOf(L.dayFromYmd(sheet.updated))) + ")";
    }
    // A house whose portal could not be read has no income to show: it is left out, and said.
    var left = state.props.filter(function (prop) { return props.indexOf(prop) < 0; })
      .map(function (prop) { return prop.label; });
    append(section, [
      // The income of a year still running is what is booked so far; the costs are the whole year's.
      el("p", { class: "hint", text: "Entrate: prenotazioni acquisite finora per il " + year + ", dai portali dei gestori " +
        "(aggiornate ogni notte). Spese: importi annui dal foglio delle spese" +
        (sheet.year === year ? "" : " del " + sheet.year + ", in attesa del foglio " + year) + revised + "." }),
      left.length ? el("p", { class: "hint left-out" }, icon("unknown"),
        el("span", { text: (left.length === 1 ? "Non compresa: " : "Non comprese: ") + left.join(", ") +
          " (dati del portale non disponibili)." })) : null,
      el("div", { class: "tiles" },
        tile("Ricavi netti", euro0(total.net_cents), props.length > 1 ? "delle " + props.length + " case" : null),
        tile("Spese annue", euro0(total.costs_cents)),
        tile("Margine", euro0(total.margin_cents), "ricavi netti − spese"),
        tile("Margine %", pct1(total.margin_ratio), "sul ricavo netto"),
        tile("Notti di pareggio", nightsCount(total.breakeven_nights), "vendute: " + F.int.format(total.nights))),
      pnlTable(props, result),
      // Each figure is rounded on its own, as in any statement kept in whole euros.
      el("p", { class: "hint rounding", text: "Importi arrotondati all'euro: un totale può differire di 1 € dalla somma delle sue voci." }),
      costBars(props, result, year)
    ]);
    return section;
  }

  function footer() {
    return el("footer", { class: "foot" },
      el("p", { text: "«Libera»: nessuna prenotazione e nessun blocco nel portale del gestore all'ultimo aggiornamento. Resta in vendita, quindi può essere prenotata da ospiti in qualsiasi momento: prima di organizzarti fai bloccare le date." }),
      el("p", { text: "«Bloccata»: date chiuse alla vendita dal proprietario o dal gestore, senza ospiti." }),
      el("p", { text: "Gli orari di check-in e check-out sono quelli della casa: un soggiorno occupa la casa dall'ora di ingresso del primo giorno all'ora di uscita dell'ultimo." }),
      state.family.note ? el("p", { text: state.family.note }) : null);
  }

  function render(focusKey) {
    var scroll = window.scrollY;
    hideTip();
    clear(root);
    if (state.pnl && !pnlSheets().length) state.pnl = false;
    root.appendChild(el("div", { class: "shell" }, header(), banners(), houseTabs(),
      state.pnl ? pnlSection()
        : [availability(), calendar(), familySection(), staysSection(), ownerSection(), footer()]));
    window.scrollTo(0, scroll);
    fitLabels();
    // Not while a sheet is open: the focus belongs inside it, and it will ask for it back.
    if (!sheet) focusByKey(focusKey);
  }

  /** Give the focus to the control with this key in the page as it is drawn now. */
  function focusByKey(key) {
    if (!key) return;
    var marked = root.querySelectorAll("[data-key]");
    for (var i = 0; i < marked.length; i++) {
      var node = marked[i];
      if (node.getAttribute("data-key") !== key) continue;
      if (node.classList.contains("day")) { // the calendar is one tab stop: move it here
        var stop = root.querySelector('.months .day[tabindex="0"]');
        if (stop) stop.tabIndex = -1;
        node.tabIndex = 0;
      }
      node.focus({ preventScroll: true });
      return;
    }
  }

  /* ---------- start ---------- */

  function boot() {
    state = blankState();
    var keys = loadKeys();
    if (!keys || keys.salt !== envelope.kdf.salt || !keys.family) {
      if (keys) clearKeys();
      renderLock();
      return;
    }
    function changed() {
      clearKeys();
      renderLock("La password è cambiata: inseriscila di nuovo.");
    }
    // A key remembered for the figures alone, from when they had their own password, opens
    // nothing any more: it is not kept.
    if (keys.owner) saveKeys({ salt: keys.salt, family: keys.family });
    C.unlock(envelope, C.b64ToBytes(keys.family)).then(function (opened) {
      if (opened) enter(opened, true); else changed();
    }).catch(changed);
  }

  boot();

  // Keep a copy of the page for when there is no connection (see sw.js). Only where the
  // browser allows it: over https, or on the machine the page is being tried on.
  if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
    navigator.serviceWorker.register("sw.js").catch(function () { /* the page works without it */ });
  }
})();
