"""Builds pages with the same structure as the owner portal, from invented data.

The repository is public, so real downloaded pages are never stored here. This
module reproduces the markup the parser depends on: the login form with its
session token, ``table.owncal`` (one row per month, 31 cells, classes ``a`` /
``s`` / ``d`` / ``b`` / ``ab`` / ``db`` / ``dis``), ``table.ownstcal`` (header,
twelve months, totals in ``tfoot``) and ``table.table-res`` (totals row made of
``th``).
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

from casecal.model import BOOKING, Stay

MONTHS = ["Gen", "Feb", "Mar", "Apr", "Mag", "Giu", "Lug", "Ago", "Set", "Ott", "Nov", "Dic"]


def eur(amount: Decimal | int | str | None) -> str:
    """Format like the portal: ``€1.234,56``; empty for a missing amount."""
    if amount is None:
        return ""
    value = Decimal(amount).quantize(Decimal("0.01"))
    sign = "-" if value < 0 else ""
    whole, cents = f"{abs(value):.2f}".split(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return f"€{sign}{'.'.join(groups)},{cents}"


def login_page(slug: str = "demo", action: str | None = None) -> str:
    action = f"/{slug}/login/" if action is None else action
    return f"""<!DOCTYPE html><html><head><title>Owners portal</title></head><body class="empty-layout">
<div class="content-center">
<form method="post" action="{action}" class="login-form">
  <h1>Log In</h1>
  <input id="f00dfeed01" class="form-control" type="hidden" name="f00dfeed01[f00dfeed01]" value="c0ffee"></input>
  <div class="form-group"><label>Property ID</label>
    <input type="text" name="property_id" value="{slug}" readonly class="form-control"></div>
  <div class="form-group"><label>Username</label>
    <input id="login-form-username" type="text" name="username" class="form-control" required></div>
  <div class="form-group"><label>Password</label>
    <input id="login-form-password" type="password" name="password" class="form-control" required></div>
  <label class="label label-danger"></label>
  <button type="submit" name="login_form" class="btn btn-secondary pull-right">Log in</button>
</form></div></body></html>"""


def _chrome(slug: str, body: str) -> str:
    return f"""<!DOCTYPE html><html><head><title>Portale</title></head><body>
<ul class="nav"><li><a href="/{slug}/property">Proprietà</a></li>
<li><a href="/{slug}/vr/stats">Statistiche</a></li>
<li><a href="/{slug}/login/logout">Logout</a></li></ul>
{body}
</body></html>"""


def landing_page(slug: str = "demo") -> str:
    return _chrome(slug, '<h2>Le mie proprietà</h2>')


def grid_classes(stays: list[Stay]) -> dict[date, str]:
    """The class attribute the portal would give to each non-free night."""
    cells: dict[date, str] = {}
    for stay in stays:
        last = stay.end - timedelta(days=1)
        night = stay.start
        while night < stay.end:
            if stay.kind == BOOKING:
                marks = ["a"] if night == stay.start else []
                if night == last:
                    marks.append("d")
                cells[night] = " ".join(marks) or "s"
            else:
                marks = ["ab"] if night == stay.start else []
                if night == last:
                    marks.append("db")
                cells[night] = " ".join(marks + ["b"])
            night += timedelta(days=1)
    return cells


def stats_page(
    year: int,
    *,
    unit_id: str = "7",
    unit_name: str = "Casa Demo",
    cells: dict[date, str] | None = None,
    kpi_columns: tuple[str, ...] = ("ADR", "Imposta", "Netto proprietario"),
    kpi_rows: dict[int, tuple[int, list]] | None = None,
    totals: list | None = None,
    slug: str = "demo",
    other_units: tuple[tuple[str, str], ...] = (),
    selected_year: int | None = None,
    selected_unit: str | None = None,
) -> str:
    """``kpi_rows`` maps a month to ``(room_nights, [amounts])``; other months are empty."""
    cells = cells or {}
    kpi_rows = kpi_rows or {}
    selected_year = year if selected_year is None else selected_year
    selected_unit = unit_id if selected_unit is None else selected_unit

    year_options = "".join(
        f'<option value="{y}"{" selected" if y == selected_year else ""}>{y}</option>'
        for y in range(year - 2, year + 2)
    )
    unit_options = "".join(
        f'<option value="{uid}"{" selected" if uid == selected_unit else ""}>{name}</option>'
        for uid, name in ((unit_id, unit_name),) + other_units
    )

    grid = ['<table class="owncal"><thead><tr><th></th>']
    grid += [f'<th class="text-center">{day}</th>' for day in range(1, 32)]
    grid.append("</tr></thead><tbody>")
    for month in range(1, 13):
        grid.append(f"<tr><th>{MONTHS[month - 1]}</th>")
        last = calendar.monthrange(year, month)[1]
        for day in range(1, 32):
            klass = "dis" if day > last else cells.get(date(year, month, day), "")
            grid.append(f'<td class="{klass}"></td>')
        grid.append("</tr>")
    grid.append("</tbody></table>")

    stats = ['<table class="ownstcal"><thead><tr><th></th><th class="text-center">Room nights</th>'
             '<th class="text-center">Occ. %</th>']
    stats += [f'<th class="text-center">{name}</th>' for name in kpi_columns]
    stats.append("</tr></thead><tbody>")
    total_nights = 0
    sums: list[Decimal] = [Decimal(0)] * len(kpi_columns)
    for month in range(1, 13):
        nights, amounts = kpi_rows.get(month, (0, [None] * len(kpi_columns)))
        total_nights += nights
        for index, amount in enumerate(amounts):
            if amount is not None:
                sums[index] += Decimal(amount)
        occupancy = round(100 * nights / calendar.monthrange(year, month)[1])
        stats.append(f'<tr><th>{MONTHS[month - 1]}</th><td class="text-center">{nights}</td>'
                     f'<td class="text-center">{occupancy}%</td>')
        stats += [f'<td class="text-center">{eur(amount)}</td>' for amount in amounts]
        stats.append("</tr>")
    stats.append("</tbody><tfoot><tr><th>Tot</th>")
    shown_totals = totals if totals is not None else [total_nights, "0%"] + [eur(value) for value in sums]
    stats += [f'<td class="text-center">{value}</td>' for value in shown_totals]
    stats.append("</tr></tfoot></table>")

    body = f"""<h3>Ricerca</h3>
<form action="/{slug}/vr/stats" method="get">
  <select name="year">{year_options}</select>
  <select name="apt">{unit_options}</select>
  <button type="submit">Ricerca</button>
</form>
{''.join(grid)}
{''.join(stats)}"""
    return _chrome(slug, body)


def reservations_page(
    start: date,
    end: date,
    headers: list[str],
    rows: list[list[str]],
    *,
    slug: str = "demo",
    shown_from: str | None = None,
    shown_to: str | None = None,
) -> str:
    """``rows`` hold the cell texts in header order; a totals row is appended like the portal's."""
    shown_from = start.strftime("%d/%m/%Y") if shown_from is None else shown_from
    shown_to = end.strftime("%d/%m/%Y") if shown_to is None else shown_to
    head = "".join(f"<th>{name}</th>" for name in headers)
    lines = []
    for row in rows:
        cells = []
        for name, value in zip(headers, row):
            if name == "Nome":
                value = f'<a href="#" onclick="viewGuest(1234); return false;">\n\t\t{value}\t\t</a>'
            elif name == "Totale pernottamento" and value.startswith("€"):
                value = "€\t\t\t\t" + value[1:] + "\t\t\t"  # the portal splits the symbol from the digits
            cells.append(f"<td>{value}</td>")
        lines.append("<tr>" + "".join(cells) + "</tr>")
    totals = "<tr><th><b>Totale</b></th><th><b>%d prenotazioni</b></th>%s</tr>" % (
        len(rows), "<th></th>" * (len(headers) - 2))
    body = f"""<form action="/{slug}/reservations" method="get">
  <input type="text" name="from" value="{shown_from}">
  <input type="text" name="to" value="{shown_to}">
  <select name="type"><option value="checkin">Arrivo</option><option value="stay" selected>Soggiorno</option></select>
  <select name="apt"><option value="">[Tutti]</option><option value="7" selected>Casa Demo</option></select>
  <button type="submit">Ricerca</button>
</form>
<table class="table table-res table-condensed"><thead><tr>{head}</tr></thead>
{''.join(lines)}
{totals}
</table>"""
    return _chrome(slug, body)
