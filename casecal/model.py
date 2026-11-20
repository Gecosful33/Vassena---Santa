"""Normalised data model shared by the scraper, the site builder and the page.

Dates follow the hospitality convention: a stay covers the nights from ``start``
(check-in day) up to, but not including, ``end`` (check-out day).

Each property manager configures their own portal, so the economic columns
differ from house to house (one shows "Imposta" and "Netto proprietario",
another adds "PM", "Proprietario", "Addebiti al proprietario"...). Amounts are
therefore kept as the portal labels them: a tuple of column names per house and
a tuple of values per row, plus the *role* of the few columns the page needs to
recognise (the average daily rate, the owner's net, the gross stay total).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Mapping

BOOKING = "booking"
CLOSED = "closed"
_KIND_TO_CODE = {BOOKING: "b", CLOSED: "c"}
_CODE_TO_KIND = {code: kind for kind, code in _KIND_TO_CODE.items()}

ROLE_ADR = "adr"
ROLE_NET = "net"
ROLE_GROSS = "gross"
_ROLES = {ROLE_ADR, ROLE_NET, ROLE_GROSS}

PAYLOAD_VERSION = 2


class DataError(ValueError):
    """Scraped data is internally inconsistent and must not be published."""


def iso_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise DataError("naive datetime: every timestamp must carry a timezone")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def to_cents(amount: Decimal | None) -> int | None:
    if amount is None:
        return None
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_cents(cents: int | None) -> Decimal | None:
    if cents is None:
        return None
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def _cents_row(values: Iterable[Decimal | None]) -> list[int | None]:
    return [to_cents(value) for value in values]


def _decimal_row(cents: Iterable[int | None]) -> tuple[Decimal | None, ...]:
    return tuple(from_cents(value) for value in cents)


@dataclass(frozen=True)
class Stay:
    """A run of consecutive nights during which the house is not free.

    ``kind`` is ``booking`` (guests) or ``closed`` (dates blocked by the owner
    or the manager, with nobody booked). ``channel`` and ``guests`` are filled
    for bookings when the portal lists them.
    """

    start: date
    end: date
    kind: str = BOOKING
    channel: str | None = None
    guests: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KIND_TO_CODE:
            raise DataError(f"unknown stay kind {self.kind!r}")
        if not self.end > self.start:
            raise DataError("a stay must end after it starts")

    @property
    def nights(self) -> int:
        return (self.end - self.start).days

    def to_row(self) -> list[Any]:
        row: list[Any] = [self.start.isoformat(), self.end.isoformat(), _KIND_TO_CODE[self.kind]]
        if self.channel is not None or self.guests is not None:
            row += [self.channel, self.guests]
        return row

    @classmethod
    def from_row(cls, row: list[Any]) -> "Stay":
        start, end, code = row[:3]
        if code not in _CODE_TO_KIND:
            raise DataError(f"unknown stay code {code!r}")
        channel, guests = (row[3], row[4]) if len(row) >= 5 else (None, None)
        return cls(date.fromisoformat(start), date.fromisoformat(end), _CODE_TO_KIND[code], channel, guests)


def stays_from_nights(nights: Mapping[date, str], breaks: Iterable[date] = ()) -> list[Stay]:
    """Merge per-night statuses into stays.

    ``nights`` maps every non-free night to its kind. Consecutive nights of the
    same kind become one stay unless the later night is listed in ``breaks``
    (a day on which a new stay is known to begin, i.e. a turnover).
    """
    turnover = set(breaks)
    stays: list[Stay] = []
    run_start: date | None = None
    previous: date | None = None
    kind = ""
    for night in sorted(nights):
        current = nights[night]
        contiguous = previous is not None and night == previous + timedelta(days=1)
        if not contiguous or current != kind or night in turnover:
            if run_start is not None and previous is not None:
                stays.append(Stay(run_start, previous + timedelta(days=1), kind))
            run_start, kind = night, current
        previous = night
    if run_start is not None and previous is not None:
        stays.append(Stay(run_start, previous + timedelta(days=1), kind))
    return stays


def normalise_stays(stays: Iterable[Stay]) -> list[Stay]:
    """Sort stays and refuse overlapping ones: an overlap means a parsing bug."""
    ordered = sorted(stays, key=lambda stay: (stay.start, stay.end))
    for earlier, later in zip(ordered, ordered[1:]):
        if later.start < earlier.end:
            raise DataError("overlapping stays")
    return ordered


@dataclass(frozen=True)
class MonthKpi:
    """One month of the portal's statistics table.

    ``values`` are the amounts of the house's ``kpi_columns``, in that order.
    """

    year: int
    month: int
    room_nights: int
    values: tuple[Decimal | None, ...] = ()
    occupancy_pct: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise DataError(f"month out of range: {self.month}")
        if self.room_nights < 0:
            raise DataError("negative room nights")

    def to_dict(self) -> dict[str, Any]:
        return {
            "y": self.year,
            "m": self.month,
            "nights": self.room_nights,
            "occ_pct": self.occupancy_pct,
            "vals": _cents_row(self.values),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MonthKpi":
        return cls(
            year=int(raw["y"]),
            month=int(raw["m"]),
            room_nights=int(raw["nights"]),
            values=_decimal_row(raw.get("vals", [])),
            occupancy_pct=raw.get("occ_pct"),
        )


@dataclass(frozen=True)
class YearTotal:
    """The statistics table's own totals row for one year.

    Kept as the portal states it: twelve amounts rounded month by month do not
    always add up to the portal's total to the cent, and an owner comparing the
    page with the portal should find the same figure.
    """

    year: int
    room_nights: int
    values: tuple[Decimal | None, ...] = ()
    occupancy_pct: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "y": self.year,
            "nights": self.room_nights,
            "occ_pct": self.occupancy_pct,
            "vals": _cents_row(self.values),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "YearTotal":
        return cls(
            year=int(raw["y"]),
            room_nights=int(raw["nights"]),
            values=_decimal_row(raw.get("vals", [])),
            occupancy_pct=raw.get("occ_pct"),
        )


@dataclass(frozen=True)
class Reservation:
    """One booking with its amounts, as listed by the portal.

    Deliberately has no guest name, nationality or contact: those columns are
    never read.
    """

    check_in: date
    check_out: date
    channel: str | None = None
    guests: int | None = None
    values: tuple[Decimal | None, ...] = ()

    def __post_init__(self) -> None:
        if not self.check_out > self.check_in:
            raise DataError("a reservation must end after it starts")

    def to_row(self) -> list[Any]:
        return [
            self.check_in.isoformat(),
            self.check_out.isoformat(),
            self.channel,
            self.guests,
            _cents_row(self.values),
        ]

    @classmethod
    def from_row(cls, row: list[Any]) -> "Reservation":
        check_in, check_out, channel, guests, cents = row
        return cls(
            date.fromisoformat(check_in), date.fromisoformat(check_out), channel, guests, _decimal_row(cents)
        )


def _check_roles(roles: Mapping[str, int], columns: tuple[str, ...], what: str) -> None:
    for role, index in roles.items():
        if role not in _ROLES:
            raise DataError(f"unknown {what} role {role!r}")
        if not 0 <= index < len(columns):
            raise DataError(f"{what} role {role!r} points outside the columns")


@dataclass
class PropertySnapshot:
    """Everything known about one house at ``fetched_at``."""

    id: str
    label: str
    fetched_at: datetime
    covered_from: date
    covered_to: date  # exclusive
    stays: list[Stay] = field(default_factory=list)
    unit: str | None = None
    kpi_columns: tuple[str, ...] = ()
    kpi_roles: dict[str, int] = field(default_factory=dict)
    kpis: list[MonthKpi] = field(default_factory=list)
    kpi_totals: list[YearTotal] = field(default_factory=list)
    reservation_columns: tuple[str, ...] = ()
    reservation_roles: dict[str, int] = field(default_factory=dict)
    reservations: list[Reservation] = field(default_factory=list)
    # House rules (HH:MM): they come from the configuration, not from the portal.
    check_in_time: str = "15:00"
    check_out_time: str = "11:00"
    # The page of the manager's portal where the owner closes dates (owner tier only).
    portal_url: str | None = None
    # Non-fatal problems met while reading this house (safe to log); never published.
    warnings: list[str] = field(default_factory=list, compare=False)

    missing = False

    def __post_init__(self) -> None:
        if not self.covered_to > self.covered_from:
            raise DataError("empty coverage window")
        self.stays = normalise_stays(self.stays)
        for stay in self.stays:
            if stay.start < self.covered_from or stay.end > self.covered_to:
                raise DataError("stay outside the covered window")

        self.kpi_columns = tuple(self.kpi_columns)
        self.reservation_columns = tuple(self.reservation_columns)
        _check_roles(self.kpi_roles, self.kpi_columns, "KPI")
        _check_roles(self.reservation_roles, self.reservation_columns, "reservation")

        seen: set[tuple[int, int]] = set()
        for kpi in self.kpis:
            key = (kpi.year, kpi.month)
            if key in seen:
                raise DataError("duplicate month in KPIs")
            seen.add(key)
            if len(kpi.values) != len(self.kpi_columns):
                raise DataError("KPI row does not match the KPI columns")
        self.kpis = sorted(self.kpis, key=lambda k: (k.year, k.month))
        if len({total.year for total in self.kpi_totals}) != len(self.kpi_totals):
            raise DataError("duplicate year in KPI totals")
        for total in self.kpi_totals:
            if len(total.values) != len(self.kpi_columns):
                raise DataError("KPI totals do not match the KPI columns")
        self.kpi_totals = sorted(self.kpi_totals, key=lambda total: total.year)

        for reservation in self.reservations:
            if len(reservation.values) != len(self.reservation_columns):
                raise DataError("reservation row does not match the reservation columns")
        self.reservations = sorted(self.reservations, key=lambda r: (r.check_in, r.check_out))

    def family_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "unit": self.unit,
            "fetched_at": iso_utc(self.fetched_at),
            "from": self.covered_from.isoformat(),
            "to": self.covered_to.isoformat(),
            "check_in": self.check_in_time,
            "check_out": self.check_out_time,
            "stays": [stay.to_row() for stay in self.stays],
        }

    def owner_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "portal_url": self.portal_url,
            "kpi_columns": list(self.kpi_columns),
            "kpi_roles": dict(self.kpi_roles),
            "kpis": [kpi.to_dict() for kpi in self.kpis],
            "kpi_totals": [total.to_dict() for total in self.kpi_totals],
            "res_columns": list(self.reservation_columns),
            "res_roles": dict(self.reservation_roles),
            "reservations": [reservation.to_row() for reservation in self.reservations],
        }

    @classmethod
    def from_dicts(
        cls, family: Mapping[str, Any], owner: Mapping[str, Any] | None
    ) -> "PropertySnapshot":
        owner = owner or {}
        return cls(
            id=family["id"],
            label=family["label"],
            unit=family.get("unit"),
            fetched_at=parse_utc(family["fetched_at"]),
            covered_from=date.fromisoformat(family["from"]),
            covered_to=date.fromisoformat(family["to"]),
            stays=[Stay.from_row(row) for row in family["stays"]],
            check_in_time=family.get("check_in", "15:00"),
            check_out_time=family.get("check_out", "11:00"),
            portal_url=owner.get("portal_url"),
            kpi_columns=tuple(owner.get("kpi_columns", ())),
            kpi_roles=dict(owner.get("kpi_roles", {})),
            kpis=[MonthKpi.from_dict(raw) for raw in owner.get("kpis", [])],
            kpi_totals=[YearTotal.from_dict(raw) for raw in owner.get("kpi_totals", [])],
            reservation_columns=tuple(owner.get("res_columns", ())),
            reservation_roles=dict(owner.get("res_roles", {})),
            reservations=[Reservation.from_row(row) for row in owner.get("reservations", [])],
        )


@dataclass
class MissingProperty:
    """A house whose portal could not be read and that has no earlier data.

    It is published as *unknown*, never as free: an empty calendar would read
    as "available all year".
    """

    id: str
    label: str

    missing = True

    def family_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "missing": True}

    def owner_dict(self) -> dict[str, Any]:
        return {"id": self.id, "missing": True}


@dataclass
class Snapshot:
    generated_at: datetime
    properties: list[PropertySnapshot | MissingProperty]
    title: str = "Calendario case"
    timezone: str = "Europe/Rome"
    note: str | None = None
    demo: bool = False
    # The family's own layer (see family.py): who uses a house when, and how the
    # page reaches the log where those entries are written. None = read-only page.
    entries: list[Any] = field(default_factory=list)
    log: dict[str, Any] | None = None

    def family_payload(self) -> dict[str, Any]:
        return {
            "v": PAYLOAD_VERSION,
            "generated_at": iso_utc(self.generated_at),
            "tz": self.timezone,
            "title": self.title,
            "note": self.note,
            "demo": self.demo,
            "properties": [prop.family_dict() for prop in self.properties],
            "entries": [entry.to_dict() for entry in self.entries],
            "log": self.log,
        }

    def owner_payload(self) -> dict[str, Any]:
        return {
            "v": PAYLOAD_VERSION,
            "properties": [prop.owner_dict() for prop in self.properties],
        }


def properties_from_payloads(
    family: Mapping[str, Any], owner: Mapping[str, Any] | None
) -> dict[str, PropertySnapshot]:
    """Rebuild the per-house snapshots carried by a previously published page."""
    if family.get("v") != PAYLOAD_VERSION:
        return {}  # written by an older layout: not worth guessing at
    owner_by_id = {raw["id"]: raw for raw in (owner or {}).get("properties", [])}
    restored: dict[str, PropertySnapshot] = {}
    for raw in family.get("properties", []):
        if raw.get("missing"):
            continue
        restored[raw["id"]] = PropertySnapshot.from_dicts(raw, owner_by_id.get(raw["id"]))
    return restored
