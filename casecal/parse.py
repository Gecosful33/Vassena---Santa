"""Turn the HTML of a Krossbooking owner portal into the normalised model.

Pure functions: no network and no clock, so every rule here is unit-tested
against markup of the same shape as the portal's.

Two things shape this module.

*The portal is not ours.* Its markup can change without notice, and a parser
that keeps going on a page it no longer understands would publish a wrong
calendar. Every function therefore checks the structure it relies on and raises
``LayoutError`` rather than guess. The yearly grid in particular is validated
against the calendar itself (the cells of days that do not exist, such as 30
February, must be the ones the portal marks as disabled) and against the
statistics table next to it (room nights per month must agree).

*Each manager configures their own columns.* Amount columns are discovered from
the table headers; nothing is read by position. Columns that are not amounts
(guest names, nationality...) are never returned, so personal data cannot leak
even if a manager adds a new column.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

from bs4 import BeautifulSoup, Tag

from .errors import LayoutError
from .model import (
    BOOKING,
    CLOSED,
    ROLE_ADR,
    ROLE_GROSS,
    ROLE_NET,
    MonthKpi,
    Reservation,
    Stay,
    YearTotal,
    stays_from_nights,
)

# Yearly grid: one cell per night.
#   a / s / d  first, middle and last night of a booking
#   b          night closed to sale; ab / db mark the first and last night of the block
#   dis        a day that does not exist in that month
_BOOKING_CLASSES = {"a", "s", "d"}
_GRID_CLASSES = _BOOKING_CLASSES | {"b", "ab", "db", "dis"}

_MONEY_RE = re.compile(r"^(-?)\s*€\s*(-?)\s*((?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})$")
_DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})\b")
_LABEL_LIMIT = 60
# Twelve monthly amounts are rounded one by one, so their sum may drift from the
# portal's own total by a cent per month.
_TOTAL_TOLERANCE = Decimal("0.12")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _text(node: Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def _label(node: Tag) -> str:
    return _text(node)[:_LABEL_LIMIT]


def _money(text: str) -> Decimal | None:
    """``€1.234,56`` -> ``Decimal('1234.56')``; ``None`` if it is not an amount."""
    match = _MONEY_RE.match(text)
    if not match:
        return None
    value = Decimal(match.group(3).replace(".", "").replace(",", "."))
    return -value if (match.group(1) or match.group(2)) else value


def _find(headers: list[str], pattern: str) -> int | None:
    regex = re.compile(pattern, re.IGNORECASE)
    for index, header in enumerate(headers):
        if regex.fullmatch(header):
            return index
    return None


# --------------------------------------------------------------------------- login


@dataclass(frozen=True)
class LoginForm:
    action: str
    fields: dict[str, str]
    submit_name: str | None


def parse_login_form(html: str) -> LoginForm:
    soup = _soup(html)
    form = soup.select_one("form.login-form")
    if form is None:
        password = soup.select_one('form input[type="password"]')
        form = password.find_parent("form") if password else None
    if form is None:
        raise LayoutError("modulo di login non trovato")
    fields = {field["name"]: field.get("value", "") for field in form.select("input[name]")}
    if "username" not in fields or "password" not in fields:
        raise LayoutError("campi del modulo di login non riconosciuti")
    submit = form.select_one('button[type="submit"][name], input[type="submit"][name]')
    return LoginForm(
        action=form.get("action") or "",
        fields=fields,
        submit_name=submit.get("name") if submit else None,
    )


def is_login_page(html: str) -> bool:
    return _soup(html).select_one('form input[type="password"][name="password"]') is not None


def is_logged_in(html: str) -> bool:
    return _soup(html).select_one('a[href*="login/logout"]') is not None


# --------------------------------------------------------------------------- units


@dataclass(frozen=True)
class Unit:
    id: str
    name: str


def parse_units(html: str) -> list[Unit]:
    select = _soup(html).select_one('select[name="apt"]')
    if select is None:
        raise LayoutError("selettore dell'appartamento non trovato")
    units = [
        Unit(option["value"], _label(option))
        for option in select.find_all("option")
        if option.get("value")
    ]
    if not units:
        raise LayoutError("nessun appartamento nell'account")
    return units


def _selected(soup: BeautifulSoup, name: str) -> str | None:
    option = soup.select_one(f'select[name="{name}"] option[selected]')
    return option.get("value") if option else None


def _require_statistics_of(soup: BeautifulSoup, year: int, unit_id: str) -> None:
    """The page must say it shows the year and the apartment that were asked for.

    Without this, a portal that ignored the query and fell back to its default
    year would have its grid silently filed under the wrong year.
    """
    if _selected(soup, "year") != str(year):
        raise LayoutError("la pagina statistiche non mostra l'anno richiesto")
    if _selected(soup, "apt") != str(unit_id):
        raise LayoutError("la pagina statistiche non mostra l'appartamento richiesto")


# --------------------------------------------------------------------------- yearly grid


@dataclass(frozen=True)
class YearGrid:
    year: int
    nights: dict[date, str]  # every non-free night -> BOOKING or CLOSED
    starts: frozenset[date]  # nights on which a booking or a block begins
    ends: frozenset[date]  # last nights of a booking or a block

    def booking_nights(self, month: int) -> int:
        return sum(1 for night, kind in self.nights.items() if night.month == month and kind == BOOKING)


def parse_year_grid(html: str, year: int, unit_id: str) -> YearGrid:
    soup = _soup(html)
    _require_statistics_of(soup, year, unit_id)
    table = soup.select_one("table.owncal")
    if table is None:
        raise LayoutError("griglia annuale non trovata")
    rows = table.select("tbody > tr")
    if len(rows) != 12:
        raise LayoutError("la griglia annuale non ha dodici mesi")

    nights: dict[date, str] = {}
    starts: set[date] = set()
    ends: set[date] = set()
    for month, row in enumerate(rows, start=1):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 31:
            raise LayoutError("un mese della griglia non ha 31 celle")
        days_in_month = calendar.monthrange(year, month)[1]
        for day, cell in enumerate(cells, start=1):
            classes = set(cell.get("class") or [])
            if classes - _GRID_CLASSES:
                raise LayoutError("la griglia contiene una classe di cella non prevista")
            if day > days_in_month:
                # Anchors rows to months and columns to days, leap years included.
                if classes != {"dis"}:
                    raise LayoutError("la griglia non corrisponde al calendario dell'anno")
                continue
            if "dis" in classes:
                raise LayoutError("la griglia non corrisponde al calendario dell'anno")
            booked = bool(classes & _BOOKING_CLASSES)
            blocked = "b" in classes
            if booked and blocked:
                raise LayoutError("una notte risulta sia prenotata sia bloccata")
            if classes & {"ab", "db"} and not blocked:
                raise LayoutError("marcatore di blocco su una notte non bloccata")
            night = date(year, month, day)
            if booked:
                nights[night] = BOOKING
                if "a" in classes:
                    starts.add(night)
                if "d" in classes:
                    ends.add(night)
            elif blocked:
                nights[night] = CLOSED
                if "ab" in classes:
                    starts.add(night)
                if "db" in classes:
                    ends.add(night)
    return YearGrid(year=year, nights=nights, starts=frozenset(starts), ends=frozenset(ends))


def stays_from_grids(grids: Iterable[YearGrid]) -> list[Stay]:
    """Join consecutive yearly grids into stays (a booking may straddle New Year)."""
    ordered = sorted(grids, key=lambda grid: grid.year)
    if not ordered:
        raise LayoutError("nessuna griglia annuale da leggere")
    for earlier, later in zip(ordered, ordered[1:]):
        if later.year != earlier.year + 1:
            raise LayoutError("le griglie annuali non sono consecutive")

    nights: dict[date, str] = {}
    starts: set[date] = set()
    ends: set[date] = set()
    for grid in ordered:
        nights.update(grid.nights)
        starts |= grid.starts
        ends |= grid.ends
    one_day = timedelta(days=1)
    stays = stays_from_nights(nights, breaks=starts | {end + one_day for end in ends})

    # A booking must show where it begins and ends, unless it runs past the
    # edge of the years that were read. Anything else means the markers changed.
    first_night = date(ordered[0].year, 1, 1)
    last_night = date(ordered[-1].year, 12, 31)
    for stay in stays:
        if stay.kind != BOOKING:
            continue
        if stay.start != first_night and stay.start not in starts:
            raise LayoutError("un soggiorno non ha il marcatore di arrivo")
        final_night = stay.end - one_day
        if final_night != last_night and final_night not in ends:
            raise LayoutError("un soggiorno non ha il marcatore di partenza")
    return stays


# --------------------------------------------------------------------------- statistics table


@dataclass(frozen=True)
class YearKpis:
    year: int
    columns: tuple[str, ...]
    roles: dict[str, int]
    months: list[MonthKpi]
    total: YearTotal | None = None  # the table's own totals row, when it has one


def _kpi_roles(columns: tuple[str, ...]) -> dict[str, int]:
    roles: dict[str, int] = {}
    adr = _find(list(columns), r"adr")
    net = _find(list(columns), r"netto\s+proprietario")
    if adr is not None:
        roles[ROLE_ADR] = adr
    if net is not None:
        roles[ROLE_NET] = net
    return roles


def parse_year_kpis(html: str, year: int, unit_id: str) -> YearKpis:
    soup = _soup(html)
    _require_statistics_of(soup, year, unit_id)
    table = soup.select_one("table.ownstcal")
    if table is None:
        raise LayoutError("tabella statistiche non trovata")
    headers = [_label(cell) for cell in table.select("thead th")]
    nights_at = _find(headers, r"room\s+nights")
    occupancy_at = _find(headers, r"occ\.?\s*%?")
    if nights_at is None or occupancy_at is None:
        raise LayoutError("colonne Room nights / Occ. % non trovate nelle statistiche")
    amount_at = [i for i in range(1, len(headers)) if i not in (nights_at, occupancy_at)]
    columns = tuple(headers[i] for i in amount_at)
    if any(not name for name in columns) or len(set(columns)) != len(columns):
        raise LayoutError("intestazioni delle statistiche non utilizzabili")
    roles = _kpi_roles(columns)

    rows = table.select("tbody > tr")
    if len(rows) != 12:
        raise LayoutError("la tabella statistiche non ha dodici mesi")
    months: list[MonthKpi] = []
    for month, row in enumerate(rows, start=1):
        cells = [_text(cell) for cell in row.find_all(["th", "td"], recursive=False)]
        if len(cells) != len(headers):
            raise LayoutError("riga delle statistiche con un numero di celle inatteso")
        if not cells[nights_at].isdigit():
            raise LayoutError("room nights non numeriche nelle statistiche")
        occupancy = cells[occupancy_at].rstrip("%").strip()
        values: list[Decimal | None] = []
        for index in amount_at:
            if not cells[index]:
                values.append(None)
                continue
            amount = _money(cells[index])
            if amount is None:
                raise LayoutError("importo non riconosciuto nelle statistiche")
            values.append(amount)
        months.append(
            MonthKpi(
                year=year,
                month=month,
                room_nights=int(cells[nights_at]),
                values=tuple(values),
                occupancy_pct=int(occupancy) if occupancy.isdigit() else None,
            )
        )

    total: YearTotal | None = None
    footer = table.select("tfoot > tr")
    if footer:
        totals = [_text(cell) for cell in footer[-1].find_all(["th", "td"], recursive=False)]
        if len(totals) != len(headers):
            raise LayoutError("riga dei totali con un numero di celle inatteso")
        if totals[nights_at].isdigit() and int(totals[nights_at]) != sum(m.room_nights for m in months):
            raise LayoutError("il totale delle room nights non torna con i mesi")
        net_role = roles.get(ROLE_NET)
        if net_role is not None:
            declared = _money(totals[amount_at[net_role]])
            summed = sum((m.values[net_role] or Decimal(0)) for m in months)
            if declared is not None and abs(declared - summed) > _TOTAL_TOLERANCE:
                raise LayoutError("il totale del netto proprietario non torna con i mesi")
        if totals[nights_at].isdigit():
            occupancy = totals[occupancy_at].rstrip("%").strip()
            total = YearTotal(
                year=year,
                room_nights=int(totals[nights_at]),
                values=tuple(_money(totals[index]) if totals[index] else None for index in amount_at),
                occupancy_pct=int(occupancy) if occupancy.isdigit() else None,
            )
    return YearKpis(year=year, columns=columns, roles=roles, months=months, total=total)


def cross_check(grid: YearGrid, kpis: YearKpis) -> None:
    """Two independent parts of the same page must tell the same story.

    If the booked nights counted in the grid differ from the room nights of
    the statistics table, one of the two is being misread: publish neither.
    """
    if grid.year != kpis.year:
        raise LayoutError("griglia e statistiche di anni diversi")
    for kpi in kpis.months:
        if grid.booking_nights(kpi.month) != kpi.room_nights:
            raise LayoutError("calendario e statistiche non coincidono sulle notti vendute")


# --------------------------------------------------------------------------- reservations


@dataclass(frozen=True)
class ReservationTable:
    columns: tuple[str, ...]
    roles: dict[str, int]
    rows: list[Reservation]


def _parse_date(text: str) -> date:
    match = _DATE_RE.match(text)
    if not match:
        raise LayoutError("data non riconosciuta nell'elenco prenotazioni")
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise LayoutError("data non valida nell'elenco prenotazioni") from exc


def format_date(day: date) -> str:
    """The portal's own date format for query parameters."""
    return day.strftime("%d/%m/%Y")


def parse_reservations(html: str, *, start: date, end: date) -> ReservationTable:
    """Reservations listed for the stays between ``start`` and ``end`` (both shown by the page).

    Only dates, channel, guest count and amount columns are returned. A column
    counts as an amount when every filled cell in it is an amount, which is what
    keeps names and other free text out whatever the manager's layout.
    """
    soup = _soup(html)
    shown_from = soup.select_one('form input[name="from"]')
    shown_to = soup.select_one('form input[name="to"]')
    if (
        shown_from is None
        or shown_to is None
        or shown_from.get("value") != format_date(start)
        or shown_to.get("value") != format_date(end)
    ):
        raise LayoutError("l'elenco prenotazioni non mostra l'intervallo richiesto")
    table = soup.select_one("table.table-res")
    if table is None:
        raise LayoutError("elenco prenotazioni non trovato")
    headers = [_label(cell) for cell in table.select("thead th")]
    check_in_at = _find(headers, r"check\s*-?\s*in")
    check_out_at = _find(headers, r"check\s*-?\s*out")
    if check_in_at is None or check_out_at is None:
        raise LayoutError("colonne Check in / Check out non trovate nell'elenco prenotazioni")
    channel_at = _find(headers, r"canale")
    guests_at = _find(headers, r"ospiti")

    # Data rows use <td>; the totals row at the bottom is made of <th>.
    lines: list[list[str]] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        if len(cells) != len(headers):
            raise LayoutError("riga dell'elenco prenotazioni con un numero di celle inatteso")
        lines.append([_text(cell) for cell in cells])

    reserved = {check_in_at, check_out_at, channel_at, guests_at}
    amount_at: list[int] = []
    for index, header in enumerate(headers):
        if index in reserved or not header:
            continue
        column = [line[index] for line in lines if line[index]]
        if column and all(_money(value) is not None for value in column):
            amount_at.append(index)
    columns = tuple(headers[i] for i in amount_at)
    if len(set(columns)) != len(columns):
        raise LayoutError("intestazioni duplicate nell'elenco prenotazioni")

    roles: dict[str, int] = {}
    gross = _find(list(columns), r"totale\s+pernottamento")
    net = _find(list(columns), r"(?:netto|tot(?:ale)?\.?)\s+proprietario")
    if gross is not None:
        roles[ROLE_GROSS] = gross
    if net is not None:
        roles[ROLE_NET] = net

    rows: list[Reservation] = []
    for line in lines:
        guests = line[guests_at] if guests_at is not None else ""
        channel = line[channel_at][:40] if channel_at is not None else ""
        rows.append(
            Reservation(
                check_in=_parse_date(line[check_in_at]),
                check_out=_parse_date(line[check_out_at]),
                channel=channel or None,
                guests=int(guests) if guests.isdigit() else None,
                values=tuple(_money(line[i]) if line[i] else None for i in amount_at),
            )
        )
    return ReservationTable(columns=columns, roles=roles, rows=rows)


def own_use_as_closed(
    stays: list[Stay], reservations: list[Reservation], *, covered_from: date, covered_to: date
) -> list[Stay]:
    """Turn the bookings that bill nothing into blocked dates.

    For a house whose manager enters the owners' own stays as bookings with
    every amount at zero. Those are not guests: in the calendar they read like
    dates closed to sale. A booking stays a booking when the portal lists no
    reservation for its dates, or lists one without any amount at all: only a
    row of explicit zeros says "nobody pays for this". And when the portal
    lists more than one row on the same dates (a booking and the one it
    replaced), a single row that bills something, or says nothing, keeps them a
    booking: taking guests for an empty house is the mistake that costs.
    """
    # True as soon as one row on those dates is anything but explicit zeros.
    billed: dict[tuple[date, date], bool] = {}
    for reservation in reservations:
        key = (max(reservation.check_in, covered_from), min(reservation.check_out, covered_to))
        amounts = [value for value in reservation.values if value is not None]
        unpaid = bool(amounts) and all(value == 0 for value in amounts)
        billed[key] = billed.get(key, False) or not unpaid
    return [
        Stay(stay.start, stay.end, CLOSED)
        if stay.kind == BOOKING and billed.get((stay.start, stay.end)) is False
        else stay
        for stay in stays
    ]


def attach_details(
    stays: list[Stay], reservations: list[Reservation], *, covered_from: date, covered_to: date
) -> tuple[list[Stay], int]:
    """Give each booking the channel and guest count of its reservation.

    Returns the stays and how many pairings failed (a booking in the calendar
    with no reservation of the same dates, or the reverse). The calendar stays
    authoritative either way.
    """
    by_dates: dict[tuple[date, date], Reservation] = {}
    for reservation in reservations:
        key = (max(reservation.check_in, covered_from), min(reservation.check_out, covered_to))
        if key[1] > key[0]:
            by_dates[key] = reservation
    detailed: list[Stay] = []
    unmatched = 0
    for stay in stays:
        if stay.kind != BOOKING:
            detailed.append(stay)
            continue
        reservation = by_dates.pop((stay.start, stay.end), None)
        if reservation is None:
            unmatched += 1
            detailed.append(stay)
        else:
            detailed.append(replace(stay, channel=reservation.channel, guests=reservation.guests))
    return detailed, unmatched + len(by_dates)
