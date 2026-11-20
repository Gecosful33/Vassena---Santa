"""Synthetic snapshot for previewing and testing the page.

Every date and amount here is invented. Nothing in this module is ever mixed
with data read from a portal: ``python -m casecal demo`` builds a separate,
visibly labelled page.

The two invented houses deliberately use different economic columns, like the
portals of two different managers do.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .family import FamilyEntry
from .model import (
    BOOKING,
    CLOSED,
    ROLE_ADR,
    ROLE_GROSS,
    ROLE_NET,
    MissingProperty,
    MonthKpi,
    PropertySnapshot,
    Reservation,
    Snapshot,
    Stay,
    YearTotal,
)

DEMO_FAMILY_PASSPHRASE = "demo-famiglia-calendario"
DEMO_OWNER_PASSPHRASE = "demo-proprieta-calendario"

_SEASON_RATE = {1: 110, 2: 110, 3: 125, 4: 150, 5: 180, 6: 250, 7: 330, 8: 410, 9: 260, 10: 150, 11: 115, 12: 170}
_SEASON_FILL = {1: 0.10, 2: 0.10, 3: 0.15, 4: 0.30, 5: 0.40, 6: 0.60, 7: 0.80, 8: 0.90, 9: 0.55, 10: 0.30, 11: 0.10, 12: 0.25}
_CENT = Decimal("0.01")
_ONE_DAY = timedelta(days=1)


def _random_stays(rng: random.Random, start: date, end: date) -> list[Stay]:
    stays: list[Stay] = []
    cursor = start
    while cursor < end:
        if rng.random() < _SEASON_FILL[cursor.month]:
            length = rng.choice((2, 3, 3, 4, 4, 5, 7))
            checkout = min(cursor + timedelta(days=length), end)
            stays.append(Stay(cursor, checkout, BOOKING))
            # Roughly one booking in three is followed by a same-day turnover.
            cursor = checkout if rng.random() < 0.35 else checkout + timedelta(days=rng.randint(1, 6))
        else:
            cursor += timedelta(days=rng.randint(2, 9))
    return stays


def _without_overlap(stays: list[Stay], fixed: list[Stay], start: date, end: date) -> list[Stay]:
    """Scripted stays win over random ones; near a year boundary they are clipped."""
    clipped = [
        Stay(max(f.start, start), min(f.end, end), f.kind)
        for f in fixed
        if min(f.end, end) > max(f.start, start)
    ]
    kept = [s for s in stays if all(s.end <= f.start or s.start >= f.end for f in clipped)]
    return sorted(kept + clipped, key=lambda stay: stay.start)


def _nights(stay: Stay) -> list[date]:
    return [stay.start + timedelta(days=offset) for offset in range(stay.nights)]


def _house(
    *,
    prop_id: str,
    label: str,
    unit: str,
    stays: list[Stay],
    years: tuple[int, ...],
    start: date,
    end: date,
    now: datetime,
    seed: int,
    with_guests: bool,
    manager_fee: Decimal,
) -> PropertySnapshot:
    """One invented house whose statistics are the pro-rata of its reservations."""
    rng = random.Random(seed)
    detailed: list[Stay] = []
    reservations: list[Reservation] = []
    # month -> [nights, gross, tax, fee, net]
    monthly: dict[tuple[int, int], list[Decimal]] = {}
    for stay in stays:
        if stay.kind != BOOKING:
            detailed.append(stay)
            continue
        channel = rng.choice(("Airbnb", "Airbnb", "Booking", "Diretta"))
        guests = rng.randint(2, 6) if with_guests else None
        detailed.append(replace(stay, channel=channel, guests=guests))
        gross = sum((Decimal(_SEASON_RATE[night.month]) for night in _nights(stay)), Decimal(0))
        ota = (gross * Decimal("0.15") if channel != "Diretta" else Decimal(0)).quantize(_CENT)
        fee = (gross * manager_fee).quantize(_CENT)
        tax = ((gross - ota - fee) * Decimal("0.21")).quantize(_CENT)
        net = gross - ota - fee - tax
        if with_guests:
            reservations.append(Reservation(stay.start, stay.end, channel, guests, (gross, tax, fee, ota, net)))
        else:
            reservations.append(Reservation(stay.start, stay.end, channel, guests, (gross, ota, net)))
        for night in _nights(stay):
            bucket = monthly.setdefault((night.year, night.month), [Decimal(0)] * 5)
            for index, amount in enumerate((Decimal(1), Decimal(_SEASON_RATE[night.month]), tax, fee, net)):
                bucket[index] += amount if index < 2 else amount / stay.nights

    kpis: list[MonthKpi] = []
    for year in years:
        for month in range(1, 13):
            bucket = monthly.get((year, month))
            if not bucket:
                kpis.append(MonthKpi(year, month, 0, (None,) * (3 if with_guests else 5)))
                continue
            count, gross, tax, fee, net = bucket
            adr = (gross / count).quantize(_CENT)
            tax, fee, net = tax.quantize(_CENT), fee.quantize(_CENT), net.quantize(_CENT)
            values = (adr, tax, net) if with_guests else (adr, fee, (net + tax).quantize(_CENT), tax, net)
            kpis.append(MonthKpi(year, month, int(count), values))

    totals: list[YearTotal] = []
    for year in years:
        rows = [kpi for kpi in kpis if kpi.year == year and kpi.room_nights]
        sold = sum(kpi.room_nights for kpi in rows)
        width = len(kpis[0].values)
        summed = [sum((kpi.values[i] for kpi in rows), Decimal(0)) if rows else None for i in range(width)]
        if rows:  # the first column is the daily rate: an average, not a sum
            summed[0] = (sum(kpi.values[0] * kpi.room_nights for kpi in rows) / sold).quantize(_CENT)
        totals.append(YearTotal(year, sold, tuple(summed)))

    if with_guests:
        kpi_columns = ("ADR", "Imposta", "Netto proprietario")
        res_columns = ("Totale pernottamento", "Imposta", "Commissioni gestore", "Commissioni OTA", "Netto proprietario")
    else:
        kpi_columns = ("ADR", "PM", "Proprietario", "Imposta", "Netto proprietario")
        res_columns = ("Totale pernottamento", "Commissioni OTA", "Tot proprietario")
    return PropertySnapshot(
        id=prop_id,
        label=label,
        unit=unit,
        fetched_at=now,
        covered_from=start,
        covered_to=end,
        stays=detailed,
        kpi_columns=kpi_columns,
        kpi_roles={ROLE_ADR: 0, ROLE_NET: len(kpi_columns) - 1},
        kpis=kpis,
        kpi_totals=totals,
        reservation_columns=res_columns,
        reservation_roles={ROLE_GROSS: 0, ROLE_NET: len(res_columns) - 1},
        reservations=reservations,
    )


def _free_run(stays: list[Stay], after: date, nights: int, limit: date) -> date | None:
    """First day on or after ``after`` from which ``nights`` nights are neither booked nor blocked."""
    day = after
    while day + timedelta(days=nights) <= limit:
        if all(s.end <= day or s.start >= day + timedelta(days=nights) for s in stays):
            return day
        day += _ONE_DAY
    return None


def _demo_entries(today: date, sea_stays: list[Stay], lake_stays: list[Stay], end: date) -> list[FamilyEntry]:
    """Invented family notes: one on dates already blocked on the portal, one on dates still for sale."""
    stamp = "2026-01-01T09:00:00Z"
    entries = []
    blocked = next((s for s in sea_stays if s.kind == CLOSED and s.start > today and s.nights >= 7), None)
    if blocked:  # inside dates the portal already shows as blocked
        entries.append(FamilyEntry("demo_blocked_1", "mare", blocked.start + _ONE_DAY, blocked.start + 6 * _ONE_DAY,
                                   "Elvio e famiglia", "", "Elvio", stamp))
    free = _free_run(lake_stays, today + 15 * _ONE_DAY, 4, end)
    if free:  # on dates still open for sale: someone has to block them
        entries.append(FamilyEntry("demo_pending_1", "lago", free, free + 4 * _ONE_DAY,
                                   "Nonna Ada", "viene con le amiche", "Giulia", stamp))
    return entries


def demo_snapshot(now: datetime, *, timezone_name: str = "Europe/Rome", variant: str = "ok") -> Snapshot:
    """Two invented houses around ``now``.

    ``variant`` exercises the warning states of the page: ``lagging`` leaves the
    second house a day and a half behind, ``missing`` drops it altogether.
    """
    today = now.astimezone(ZoneInfo(timezone_name)).date()
    years = (today.year, today.year + 1)
    start, end = date(years[0], 1, 1), date(years[1] + 1, 1, 1)
    day = _ONE_DAY

    sea_fixed = [
        Stay(today - 2 * day, today + 3 * day, BOOKING),  # guests in the house today
        Stay(today + 3 * day, today + 7 * day, BOOKING),  # same-day turnover
        Stay(today + 21 * day, today + 35 * day, CLOSED),  # dates blocked by the owner
    ]
    lake_fixed = [
        Stay(today - 6 * day, today, BOOKING),  # checked out this morning
        Stay(today + 5 * day, today + 9 * day, BOOKING),
        Stay(today + 12 * day, today + 14 * day, CLOSED),
    ]
    sea_stays = _without_overlap(
        _random_stays(random.Random(11), start, end - 40 * day), sea_fixed, start, end
    )
    lake_stays = _without_overlap(
        _random_stays(random.Random(29), start, end - 90 * day), lake_fixed, start, end
    )

    common = dict(years=years, start=start, end=end)
    sea = _house(
        prop_id="mare", label="Casa Mare", unit="Bilocale demo", stays=sea_stays, now=now,
        seed=3, with_guests=True, manager_fee=Decimal("0.20"), **common,
    )
    lake: PropertySnapshot | MissingProperty = _house(
        prop_id="lago", label="Casa Lago", unit="Villa demo", stays=lake_stays,
        now=now - timedelta(hours=36) if variant == "lagging" else now,
        seed=5, with_guests=False, manager_fee=Decimal("0.18"), **common,
    )
    if variant == "missing":
        lake = MissingProperty(id="lago", label="Casa Lago")
    # In the "missing" variant the lake house keeps its family note: the diary does not
    # depend on the portal, the note just cannot be checked against it.
    entries = _demo_entries(today, sea_stays, lake_stays, end)
    return Snapshot(
        generated_at=now,
        properties=[sea, lake],
        title="Calendario case",
        timezone=timezone_name,
        note=None,
        demo=True,
        entries=entries,
        # Edits made in the demo stay in the browser that made them.
        log={"kind": "local"},
    )
