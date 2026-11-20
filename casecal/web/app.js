/* DOM layer: lock screen, calendar, stays and owner area.
 * Labels come from the managers' portals and are therefore untrusted: every
 * string is inserted with textContent, never parsed as markup. Positions are
 * set through the CSSOM because the page's CSP forbids inline style attributes. */
(function () {
  "use strict";

  var C = CaseCalCrypto, L = CaseCalLogic;
  var SVG_NS = "http://www.w3.org/2000/svg";
  var STORE = "casecal.keys.v1";
  var WINDOWS_SHOWN = 4;
  var IT = "it-IT";

  var root = document.getElementById("app");
  var envelope = JSON.parse(document.getElementById("casecal-data").textContent);
  var genericTitle = document.title;
  var state = null;

  function blankState() {
    return {
      family: null, owner: null, props: [], today: 0, filter: "all", year: null,
      showPast: false, pastStays: {}, allWindows: {}, remembered: false
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
    critical: "M8 2.5l6 10.5H2zM8 6.6v2.9M8 11.4v.2"
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
    month: dtf({ timeZone: "UTC", month: "long", year: "numeric" }),
    monthName: dtf({ timeZone: "UTC", month: "long" }),
    monthShort: dtf({ timeZone: "UTC", month: "short" }),
    // Italian omits the thousands separator on four-digit numbers by default; in a
    // table of amounts "1.350,00" lines up and reads better than "1350,00".
    money: new Intl.NumberFormat(IT, { style: "currency", currency: "EUR", useGrouping: "always" }),
    money0: new Intl.NumberFormat(IT, { style: "currency", currency: "EUR", maximumFractionDigits: 0, useGrouping: "always" }),
    pct: new Intl.NumberFormat(IT, { style: "percent", maximumFractionDigits: 0 }),
    int: new Intl.NumberFormat(IT, { useGrouping: "always" })
  };
  var DOW = ["L", "M", "M", "G", "V", "S", "D"];

  function dateOf(day) { return new Date(day * L.DAY_MS); }
  function sameYear(day) { return L.parts(day).y === L.parts(state.today).y; }
  function dayLabel(day) { return (sameYear(day) ? F.day : F.dayYear).format(dateOf(day)); }
  function shortLabel(day) { return (sameYear(day) ? F.short : F.shortYear).format(dateOf(day)); }
  function nights(n) { return n === 1 ? "1 notte" : F.int.format(n) + " notti"; }
  function span(a, b) { return shortLabel(a) + " → " + shortLabel(b); }
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
      el("p", { text: notice || "Inserisci la password di famiglia." }),
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
          saveKeys({
            salt: envelope.kdf.salt,
            family: C.bytesToB64(opened.familyKey),
            owner: opened.ownerKey ? C.bytesToB64(opened.ownerKey) : null
          });
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
    state = blankState();
    renderLock();
  }

  function enter(opened, remembered) {
    state = blankState();
    state.family = opened.family;
    state.owner = opened.owner;
    state.remembered = remembered;
    state.props = opened.family.properties.map(L.prepare);
    state.props.forEach(function (prop, index) { prop.index = index; });
    state.today = L.todayInZone(opened.family.tz, Date.now());
    document.title = opened.family.title || genericTitle;
    render();
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

  /** Hover, focus and tap all open the same readout for the marks inside `container`. */
  function attachTips(container, selector, build) {
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
      if (node) showTip(node, build(node));
    });
    container.addEventListener("focusout", hideTip);
    container.addEventListener("click", function (event) {
      var node = target(event);
      if (!node) return;
      // A second tap on the same mark closes its readout. "Same" is judged by what was
      // open when the finger went down: the tap's own focus event has already reopened it.
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

  /* ---------- shared pieces ---------- */

  function visible() {
    return state.props.filter(function (prop) { return state.filter === "all" || prop.id === state.filter; });
  }
  function readable() { return visible().filter(function (prop) { return !prop.missing; }); }

  function segmented(name, label, options, current, onPick) {
    return el("div", { class: "seg", role: "radiogroup", "aria-label": label }, options.map(function (option) {
      var input = el("input", {
        type: "radio", name: name, value: String(option.id), "data-key": name + ":" + option.id,
        checked: option.id === current
      });
      input.addEventListener("change", function () { onPick(option.id, name + ":" + option.id); });
      var face = el("span", option.index === undefined ? null : { class: "p" + option.index },
        option.index === undefined ? null : el("i", { class: "swatch" }), option.label);
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

  /** Words for one house on one calendar day (tooltip and screen reader share them). */
  function describe(prop, day) {
    var info = L.dayInfo(prop, day);
    var detail = "";
    if (info.state === "free") {
      var status = L.nightStatus(prop, day);
      var until = status.open ? "nessuna prenotazione successiva"
        : "fino a " + shortLabel(status.until) + ", " + nights(status.until - day);
      detail = (info.leaving ? "dal pomeriggio (check-out al mattino), " : "") + until;
    } else if (info.state === "booking" || info.state === "closed") {
      detail = span(info.night.start, info.night.end) + ", " + nights(info.night.end - info.night.start);
      if (info.night.start === day) detail += info.leaving ? ", cambio in giornata" : ", inizio in giornata";
    }
    return { status: STATE_TEXT[info.state], detail: detail };
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

  function filterBar() {
    if (state.props.length < 2) return null;
    var options = [{ id: "all", label: "Tutte" }].concat(state.props.map(function (prop) {
      return { id: prop.id, label: prop.label, index: prop.index };
    }));
    return el("div", null, segmented("filter", "Case da mostrare", options, state.filter, function (id, key) {
      state.filter = id;
      render(key);
    }));
  }

  function nowLine(prop) {
    var status = L.nightStatus(prop, state.today);
    var title, rest;
    if (status.state === "free") {
      title = "Libera stanotte";
      rest = status.open ? "nessuna prenotazione successiva"
        : "fino a " + dayLabel(status.until) + " (" + nights(status.until - state.today) + ")";
    } else if (status.state === "unknown") {
      title = "Dato non disponibile";
      rest = "il calendario del gestore non copre la data di oggi";
    } else {
      title = status.state === "closed" ? "Bloccata stanotte" : "Ospiti in casa stanotte";
      rest = (status.state === "closed" ? "chiusa alla vendita, senza ospiti · " : "") +
        (status.until === null ? "nessuna notte libera nel periodo noto" : "prima notte libera " + dayLabel(status.until));
    }
    return el("p", { class: "now" }, icon(status.state), el("span", null, el("strong", { text: title }), " · " + rest));
  }

  function houseCard(prop) {
    var card = el("article", { class: "card house p" + prop.index },
      el("div", { class: "house-head" }, el("i", { class: "swatch" }), el("h3", { text: prop.label }),
        prop.unit ? el("span", { class: "unit", text: prop.unit }) : null));
    if (prop.missing) {
      append(card, el("p", { class: "now" }, icon("unknown"),
        el("span", null, el("strong", { text: "Dati non disponibili" }), " · il portale del gestore non è stato letto")));
      return card;
    }
    append(card, nowLine(prop));
    append(card, el("h4", { text: "Prossimi periodi liberi" }));
    var windows = L.freeWindows(prop, state.today);
    if (!windows.length) {
      append(card, el("p", { class: "empty", text: "Nessun periodo libero fino a " + shortLabel(prop.to - 1) + "." }));
      return card;
    }
    var expanded = !!state.allWindows[prop.id];
    var shown = expanded ? windows : windows.slice(0, WINDOWS_SHOWN);
    append(card, el("table", { class: "rows" },
      el("thead", null, el("tr", null,
        el("th", { scope: "col", text: "Dal" }), el("th", { scope: "col", text: "Al" }),
        el("th", { scope: "col", class: "num", text: "Notti" }))),
      el("tbody", null, shown.map(function (w) {
        return el("tr", null,
          el("td", { text: w.start === state.today ? "oggi" : dayLabel(w.start) }),
          el("td", { text: w.open ? "in poi" : dayLabel(w.end) }),
          el("td", { class: "num", text: w.open ? "–" : F.int.format(w.nights) }));
      }))));
    if (windows.length > WINDOWS_SHOWN) {
      var key = "windows:" + prop.id;
      append(card, el("button", {
        class: "btn quiet more", type: "button", "data-key": key, "aria-expanded": String(expanded),
        text: expanded ? "Mostra meno" : "Mostra tutti i " + windows.length + " periodi",
        onclick: function () { state.allWindows[prop.id] = !expanded; render(key); }
      }));
    }
    return card;
  }

  function availability() {
    return el("section", { class: "houses", "aria-label": "Disponibilità" }, visible().map(houseCard));
  }

  function legend(props, withStates) {
    return el("div", { class: "legend" },
      props.map(function (prop) {
        return el("span", { class: "p" + prop.index }, el("i", { class: "swatch" }),
          prop.label + (withStates ? ": ospiti" : ""));
      }),
      withStates ? el("span", null,
        el("span", { class: "pair" }, props.map(function (prop) { return el("i", { class: "swatch closed p" + prop.index }); })),
        "Bloccata: linea sottile (chiusa alla vendita, senza ospiti)") : null,
      withStates ? el("span", null, el("i", { class: "swatch free" }), "Libera (nessuna barra)") : null);
  }

  function dayTip(day, props) {
    return [el("div", { class: "tip-title", text: F.long.format(dateOf(day)) })].concat(props.map(function (prop) {
      var words = describe(prop, day);
      return el("div", { class: "tip-row p" + prop.index },
        el("i", { class: "key" }),
        el("span", null, el("strong", { text: words.status }), el("span", { class: "who", text: " · " + prop.label })),
        words.detail ? el("span", { class: "detail", text: words.detail }) : null);
    }));
  }

  function monthBlock(month, props, focusDay) {
    var first = L.dayOf(month.y, month.m, 1);
    var block = el("section", { class: "month" + (first + L.daysInMonth(month.y, month.m) <= state.today ? " past" : "") },
      el("h3", { text: F.month.format(dateOf(first)) }),
      el("div", { class: "dow", "aria-hidden": "true" }, DOW.map(function (letter) { return el("span", { text: letter }); })));
    L.monthWeeks(month.y, month.m).forEach(function (week) {
      var cells = el("div", { class: "cells" }, week.map(function (day, column) {
        if (day === null) return el("span", { class: "pad" + (column >= 5 ? " we" : "") });
        var spoken = F.long.format(dateOf(day)) + ". " + props.map(function (prop) {
          var words = describe(prop, day);
          return prop.label + ": " + words.status + (words.detail ? ", " + words.detail : "");
        }).join(". ") + ".";
        return el("button", {
          type: "button", "data-day": String(day), tabindex: day === focusDay ? "0" : "-1", "aria-label": spoken,
          class: "day" + (column >= 5 ? " we" : "") + (day < state.today ? " past" : "") + (day === state.today ? " today" : "")
        }, el("span", { class: "n", text: String(L.parts(day).d) }));
      }));
      var lanes = el("div", { class: "lanes", "aria-hidden": "true" }, props.map(function (prop) {
        return el("div", { class: "lane p" + prop.index }, L.weekSegments(prop.stays, week).map(function (segment) {
          var bar = el("i", {
            class: "bar" + (segment.kind === "closed" ? " closed" : "") + (segment.cutLeft ? " cut-l" : "") +
              (segment.cutRight ? " cut-r" : "") + (segment.stay.end <= state.today ? " past" : "")
          });
          // 1px inset on every real end leaves the 2px surface gap between touching stays.
          var insetLeft = segment.cutLeft ? 0 : 1, insetRight = segment.cutRight ? 0 : 1;
          bar.style.left = "calc(" + (segment.left / 7 * 100).toFixed(4) + "% + " + insetLeft + "px)";
          bar.style.width = "calc(" + ((segment.right - segment.left) / 7 * 100).toFixed(4) + "% - " + (insetLeft + insetRight) + "px)";
          return bar;
        }));
      }));
      block.appendChild(el("div", { class: "week" }, cells, lanes));
    });
    return block;
  }

  function calendar() {
    var props = readable();
    var section = el("section", { class: "card" });
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
    append(section, legend(props, true));
    var today = L.parts(state.today);
    var to = Math.max.apply(null, props.map(function (prop) { return prop.to; }));
    var from = state.showPast ? Math.min.apply(null, props.map(function (prop) { return prop.from; }))
      : L.dayOf(today.y, today.m, 1);
    var months = L.monthsBetween(from, to);
    var focusDay = state.today >= from && state.today < to ? state.today : from;
    var grid = el("div", { class: "months" + (props.length === 1 ? " single" : "") },
      months.map(function (month) { return monthBlock(month, props, focusDay); }));
    var laneHeight = props.length === 1 ? 12 : 8;
    grid.style.setProperty("--lanes-h", (props.length * laneHeight + (props.length - 1) * 3) + "px");

    attachTips(grid, ".day", function (node) { return dayTip(Number(node.getAttribute("data-day")), props); });
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
  function stayRows(stays) {
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
    return el("table", { class: "rows" },
      el("thead", null, el("tr", null,
        el("th", { scope: "col", text: "Dal" }), el("th", { scope: "col", text: "Al" }),
        el("th", { scope: "col", class: "num", text: "Notti" }), el("th", { scope: "col", text: "Stato" }))),
      el("tbody", null, rows));
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
          upcoming.length ? stayRows(upcoming) : el("p", { class: "empty", text: "Nessun soggiorno in programma." }),
          past.length ? el("button", {
            class: "btn quiet more", type: "button", "data-key": key, "aria-expanded": String(open),
            text: open ? "Nascondi i soggiorni passati" : "Mostra i soggiorni passati (" + past.length + ")",
            onclick: function () { state.pastStays[prop.id] = !open; render(key); }
          }) : null,
          open && past.length ? stayRows(past) : null);
      })));
  }

  /* ---------- owner area ---------- */

  function ownerLock() {
    var field = passwordField("owner-pw", "Password proprietà", "off");
    var remember = el("input", { type: "checkbox" });
    var error = el("p", { class: "error", role: "alert", hidden: true });
    var submit = el("button", { class: "btn primary", type: "submit", text: "Sblocca" });
    var form = el("form", { class: "owner-lock", novalidate: true },
      el("p", { class: "hint", text: "Ricavi e indicatori economici: serve la seconda password." }),
      field.node, el("label", { class: "check" }, remember, "Ricorda su questo dispositivo"), error, submit);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!field.input.value.trim()) { showError(error, "Scrivi la password."); return; }
      submit.disabled = true;
      submit.textContent = "Verifica in corso…";
      showError(error, "");
      var derived;
      C.deriveKey(field.input.value, envelope.kdf).then(function (key) {
        derived = key;
        return C.unlockOwner(envelope, key);
      }).then(function (owner) {
        if (!owner) throw new Error("Password non corretta.");
        state.owner = owner;
        if (remember.checked) {
          var keys = loadKeys();
          if (!keys || keys.salt !== envelope.kdf.salt) {
            keys = { salt: envelope.kdf.salt, family: owner.family_key };
          }
          keys.owner = C.bytesToB64(derived);
          saveKeys(keys);
        }
        render("owner-close");
      }).catch(function (err) {
        submit.disabled = false;
        submit.textContent = "Sblocca";
        showError(error, err && err.message ? err.message : "Impossibile sbloccare.");
        field.input.select();
      });
    });
    return form;
  }

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
          return el("div", { class: "tip-row p" + s.prop.index }, el("i", { class: "key" }),
            el("span", null, el("span", { class: "val", text: euro(s.rows[month - 1].net_cents) }),
              el("span", { class: "who", text: " · " + s.prop.label })));
        }));
    });
    var months = [];
    for (var n = 1; n <= 12; n++) months.push(el("span", { text: F.monthShort.format(dateOf(L.dayOf(year, n, 1))).replace(".", "") }));
    return el("figure", { class: "chart" },
      el("figcaption", { text: "Netto proprietario per mese · " + year }),
      series.length > 1 ? legend(series.map(function (s) { return s.prop; }), false) : null,
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
  /** A wide table scrolls sideways inside its own box; its first column stays in view. */
  function wideTable(columns, body, footer, extraClass) {
    var table = el("table", { class: "rows kpi" + (extraClass ? " " + extraClass : "") },
      el("thead", null, headerRow(columns)),
      el("tbody", null, body),
      footer ? el("tfoot", null, footer) : null);
    table.style.minWidth = (columns.length * 86) + "px";
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
    if (withRatio) columns.push({ name: "Netto/lordo*", numeric: true });

    function line(label, nightCount, occupancy, vals, gross, net) {
      var out = [label, F.int.format(nightCount), pct(occupancy)];
      vals.forEach(function (value, i) {
        out.push(euro(value));
        if (i === adrAt) out.push(euro(gross));
      });
      if (withRatio) out.push(pct(gross && net !== null ? net / gross : null));
      return out;
    }
    var total = entry.house;
    return [
      el("h4", { text: "Statistiche mensili" }),
      wideTable(columns,
        entry.rows.map(function (row) {
          return bodyRow(columns, line(F.monthName.format(dateOf(L.dayOf(year, row.m, 1))), row.nights,
            row.occupancy, row.vals, row.gross_cents, row.net_cents), row.nights ? null : "zero");
        }),
        bodyRow(columns, line("Totale " + year, total.nights, total.occupancy, total.vals, total.gross_cents, total.net_cents)),
        "months"),
      withGross ? el("p", { class: "hint", text: "* calcolato: lordo = notti × ADR" }) : null
    ];
  }

  /** The bookings checked in during the year, with the amounts the portal lists for each. */
  function reservationTable(year, entry) {
    var data = L.yearReservations(entry.owner, year);
    if (!data.rows.length) return null;
    var names = entry.owner.res_columns || [];
    var withChannel = data.rows.some(function (row) { return !!row.channel; });
    var withGuests = data.rows.some(function (row) { return row.guests !== null && row.guests !== undefined; });
    var columns = [{ name: "Check-in", date: true }, { name: "Check-out", date: true }, { name: "Notti", numeric: true }];
    if (withChannel) columns.push({ name: "Canale" });
    if (withGuests) columns.push({ name: "Ospiti", numeric: true });
    names.forEach(function (name) { columns.push({ name: name, numeric: true }); });

    function line(first, second, nightCount, channel, guests, vals) {
      var out = [first, second, F.int.format(nightCount)];
      if (withChannel) out.push(channel || "");
      if (withGuests) out.push(guests === null || guests === undefined ? "" : String(guests));
      return out.concat(vals.map(euro));
    }
    return [
      el("h4", { text: "Prenotazioni con check-in nel " + year + " (" + data.rows.length + ")" }),
      wideTable(columns,
        data.rows.map(function (row) {
          return bodyRow(columns, line(F.day.format(dateOf(row.start)), F.day.format(dateOf(row.end)),
            row.nights, row.channel, row.guests, row.vals));
        }),
        bodyRow(columns, line("Totale", "", data.nights, "", null, data.totals)))
    ];
  }

  function ownerSection() {
    var section = el("section", { class: "card" });
    if (!state.owner) {
      append(section, [el("div", { class: "section-head" }, el("h2", { text: "Area proprietà" })), ownerLock()]);
      return section;
    }
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
        function (id, key) { state.year = id; render(key); }) : null,
      el("button", {
        class: "btn quiet", type: "button", "data-key": "owner-close", text: "Chiudi area",
        onclick: function () {
          state.owner = null;
          var keys = loadKeys();
          if (keys && keys.owner) { keys.owner = null; saveKeys(keys); }
          render();
        }
      })));
    if (!years.length) {
      append(section, el("p", { class: "empty", text: "Nessun dato economico disponibile per le case selezionate." }));
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
          kpiTable(year, entry), reservationTable(year, entry));
      }))
    ]);
    return section;
  }

  function footer() {
    return el("footer", { class: "foot" },
      el("p", { text: "«Libera»: nessuna prenotazione e nessun blocco nel portale del gestore all'ultimo aggiornamento. Resta in vendita, quindi può essere prenotata da ospiti in qualsiasi momento: prima di organizzarti fai bloccare le date." }),
      el("p", { text: "«Bloccata»: date chiuse alla vendita dal proprietario o dal gestore, senza ospiti." }),
      state.family.note ? el("p", { text: state.family.note }) : null);
  }

  function render(focusKey) {
    var scroll = window.scrollY;
    hideTip();
    clear(root);
    root.appendChild(el("div", { class: "shell" },
      header(), banners(), filterBar(), availability(), calendar(), staysSection(), ownerSection(), footer()));
    window.scrollTo(0, scroll);
    if (!focusKey) return;
    var marked = root.querySelectorAll("[data-key]");
    for (var i = 0; i < marked.length; i++) {
      if (marked[i].getAttribute("data-key") === focusKey) { marked[i].focus({ preventScroll: true }); break; }
    }
  }

  /* ---------- start ---------- */

  function boot() {
    state = blankState();
    var keys = loadKeys();
    if (!keys || keys.salt !== envelope.kdf.salt || !(keys.owner || keys.family)) {
      if (keys) clearKeys();
      renderLock();
      return;
    }
    var attempts = [keys.owner, keys.family].filter(Boolean);
    (function next() {
      if (!attempts.length) {
        clearKeys();
        renderLock("La password è cambiata: inseriscila di nuovo.");
        return;
      }
      C.unlock(envelope, C.b64ToBytes(attempts.shift())).then(function (opened) {
        if (opened) enter(opened, true); else next();
      }).catch(next);
    })();
  }

  boot();
})();
