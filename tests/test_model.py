from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from casecal.model import (
    BOOKING,
    CLOSED,
    DataError,
    MissingProperty,
    MonthKpi,
    PropertySnapshot,
    Reservation,
    Snapshot,
    Stay,
    YearTotal,
    iso_utc,
    normalise_stays,
    properties_from_payloads,
    stays_from_nights,
    to_cents,
)

NOW = datetime(2026, 8, 15, 2, 17, tzinfo=timezone.utc)


def d(text: str) -> date:
    return date.fromisoformat(text)


def snapshot(**overrides) -> PropertySnapshot:
    values = dict(
        id="santa",
        label="Santa",
        unit="Bilocale",
        fetched_at=NOW,
        covered_from=d("2026-01-01"),
        covered_to=d("2028-01-01"),
        stays=[
            Stay(d("2026-08-07"), d("2026-08-10"), channel="Airbnb", guests=4),
            Stay(d("2026-08-01"), d("2026-08-07")),
            Stay(d("2026-09-01"), d("2026-09-08"), CLOSED),
        ],
        kpi_columns=("ADR", "Imposta", "Netto proprietario"),
        kpi_roles={"adr": 0, "net": 2},
        kpis=[
            MonthKpi(2026, 8, 9, (Decimal("415.87"), Decimal("786.00"), Decimal("1560.52")), 29),
            MonthKpi(2026, 7, 0, (None, None, None)),
        ],
        kpi_totals=[YearTotal(2026, 9, (Decimal("415.87"), Decimal("786.00"), Decimal("1560.51")), 2)],
        reservation_columns=("Totale pernottamento", "Netto proprietario"),
        reservation_roles={"gross": 0, "net": 1},
        reservations=[
            Reservation(d("2026-08-07"), d("2026-08-10"), "Airbnb", 4, (Decimal("1247.61"), Decimal("520.17"))),
            Reservation(d("2026-08-01"), d("2026-08-07"), None, None, (Decimal("2495.22"), Decimal("1040.35"))),
        ],
    )
    values.update(overrides)
    return PropertySnapshot(**values)


def test_stay_counts_nights_and_rejects_nonsense():
    assert Stay(d("2026-08-01"), d("2026-08-07")).nights == 6
    with pytest.raises(DataError):
        Stay(d("2026-08-07"), d("2026-08-07"))
    with pytest.raises(DataError):
        Stay(d("2026-08-07"), d("2026-08-01"))
    with pytest.raises(DataError):
        Stay(d("2026-08-01"), d("2026-08-02"), "maybe")


def test_nights_merge_into_stays():
    nights = {
        d("2026-08-01"): BOOKING,
        d("2026-08-02"): BOOKING,
        d("2026-08-03"): CLOSED,  # kind changes
        d("2026-08-04"): CLOSED,
        d("2026-08-10"): BOOKING,  # gap
    }
    assert stays_from_nights(nights) == [
        Stay(d("2026-08-01"), d("2026-08-03"), BOOKING),
        Stay(d("2026-08-03"), d("2026-08-05"), CLOSED),
        Stay(d("2026-08-10"), d("2026-08-11"), BOOKING),
    ]
    assert stays_from_nights({}) == []


def test_a_known_turnover_splits_consecutive_nights():
    nights = {d("2026-08-0%d" % day): BOOKING for day in range(1, 7)}
    assert stays_from_nights(nights, breaks=[d("2026-08-04")]) == [
        Stay(d("2026-08-01"), d("2026-08-04")),
        Stay(d("2026-08-04"), d("2026-08-07")),
    ]


def test_back_to_back_stays_are_fine_overlaps_are_not():
    a, b = Stay(d("2026-08-01"), d("2026-08-07")), Stay(d("2026-08-07"), d("2026-08-10"))
    assert normalise_stays([b, a]) == [a, b]
    with pytest.raises(DataError):
        normalise_stays([a, Stay(d("2026-08-06"), d("2026-08-09"))])


def test_snapshot_sorts_and_validates():
    snap = snapshot()
    assert [s.start for s in snap.stays] == [d("2026-08-01"), d("2026-08-07"), d("2026-09-01")]
    assert [(k.year, k.month) for k in snap.kpis] == [(2026, 7), (2026, 8)]
    assert [r.check_in for r in snap.reservations] == [d("2026-08-01"), d("2026-08-07")]
    with pytest.raises(DataError):
        snapshot(stays=[Stay(d("2025-12-30"), d("2026-01-02"))])
    with pytest.raises(DataError):
        snapshot(stays=[Stay(d("2027-12-30"), d("2028-01-02"))])
    with pytest.raises(DataError, match="duplicate"):
        snapshot(kpis=[MonthKpi(2026, 8, 1, (None,) * 3), MonthKpi(2026, 8, 2, (None,) * 3)])
    with pytest.raises(DataError):
        snapshot(covered_to=d("2026-01-01"))


def test_rows_must_match_the_columns_they_claim():
    with pytest.raises(DataError, match="KPI row"):
        snapshot(kpis=[MonthKpi(2026, 8, 1, (Decimal("1"),))])
    with pytest.raises(DataError, match="reservation row"):
        snapshot(reservations=[Reservation(d("2026-08-01"), d("2026-08-07"), None, None, ())])
    with pytest.raises(DataError, match="KPI totals"):
        snapshot(kpi_totals=[YearTotal(2026, 9, ())])
    with pytest.raises(DataError, match="duplicate year"):
        snapshot(kpi_totals=[YearTotal(2026, 9, (None,) * 3), YearTotal(2026, 9, (None,) * 3)])
    with pytest.raises(DataError, match="points outside"):
        snapshot(kpi_roles={"net": 3})
    with pytest.raises(DataError, match="unknown KPI role"):
        snapshot(kpi_roles={"profit": 0})
    with pytest.raises(DataError):
        Reservation(d("2026-08-07"), d("2026-08-07"))


def test_a_stay_may_end_on_the_last_covered_day():
    snap = snapshot(stays=[Stay(d("2027-12-28"), d("2028-01-01"))])
    assert snap.stays[0].nights == 4


def test_money_is_rounded_half_up_to_cents():
    assert to_cents(Decimal("259.64")) == 25964
    assert to_cents(Decimal("0.005")) == 1
    assert to_cents(Decimal("1833.98")) == 183398
    assert to_cents(None) is None
    with pytest.raises(DataError):
        MonthKpi(2026, 13, 1)
    with pytest.raises(DataError):
        MonthKpi(2026, 1, -1)


def test_stays_with_details_still_sort_and_compare():
    a = Stay(d("2026-08-01"), d("2026-08-07"), channel="Airbnb", guests=None)
    b = Stay(d("2026-08-07"), d("2026-08-10"))
    assert normalise_stays([b, a]) == [a, b]
    assert Stay.from_row(a.to_row()) == a and Stay.from_row(b.to_row()) == b


def test_timestamps_must_be_timezone_aware():
    assert iso_utc(NOW) == "2026-08-15T02:17:00Z"
    with pytest.raises(DataError):
        iso_utc(datetime(2026, 8, 15, 2, 17))


def test_the_family_payload_carries_no_economics():
    payload = Snapshot(generated_at=NOW, properties=[snapshot()]).family_payload()
    house = payload["properties"][0]
    assert set(house) == {"id", "label", "unit", "fetched_at", "from", "to", "check_in", "check_out", "stays"}
    assert (house["check_in"], house["check_out"]) == ("15:00", "11:00")
    assert house["stays"] == [
        ["2026-08-01", "2026-08-07", "b"],
        ["2026-08-07", "2026-08-10", "b", "Airbnb", 4],  # channel and guests, never amounts
        ["2026-09-01", "2026-09-08", "c"],
    ]
    flat = repr(payload)
    for leaked in ("415", "1560", "1247", "520", "kpis", "reservations", "Netto"):
        assert leaked not in flat


def test_the_owner_payload_carries_the_economics_in_cents():
    payload = Snapshot(generated_at=NOW, properties=[snapshot()]).owner_payload()
    house = payload["properties"][0]
    assert house["kpi_columns"] == ["ADR", "Imposta", "Netto proprietario"]
    assert house["kpi_roles"] == {"adr": 0, "net": 2}
    assert house["kpis"][1] == {"y": 2026, "m": 8, "nights": 9, "occ_pct": 29, "vals": [41587, 78600, 156052]}
    assert house["kpis"][0]["vals"] == [None, None, None]
    assert house["kpi_totals"] == [{"y": 2026, "nights": 9, "occ_pct": 2, "vals": [41587, 78600, 156051]}]
    assert house["res_columns"] == ["Totale pernottamento", "Netto proprietario"]
    assert house["res_roles"] == {"gross": 0, "net": 1}
    assert house["reservations"][1] == ["2026-08-07", "2026-08-10", "Airbnb", 4, [124761, 52017]]


def test_the_portal_link_is_for_the_owner_only():
    linked = snapshot(portal_url="https://portal.example/gestore/property/details/id/7")
    published = Snapshot(generated_at=NOW, properties=[linked])
    assert published.owner_payload()["properties"][0]["portal_url"] == linked.portal_url
    assert "portal.example" not in repr(published.family_payload())  # it names the manager
    restored = properties_from_payloads(published.family_payload(), published.owner_payload())["santa"]
    assert restored.portal_url == linked.portal_url
    assert properties_from_payloads(published.family_payload(), None)["santa"].portal_url is None


def test_the_family_layer_travels_in_the_family_payload():
    plain = Snapshot(generated_at=NOW, properties=[snapshot()])
    assert plain.family_payload()["entries"] == [] and plain.family_payload()["log"] is None
    assert set(plain.owner_payload()) == {"v", "properties"}  # nothing of it in the owner tier


def test_published_payloads_restore_the_same_snapshot():
    original = snapshot()
    published = Snapshot(generated_at=NOW, properties=[original, MissingProperty("vassena", "Vassena")])
    restored = properties_from_payloads(published.family_payload(), published.owner_payload())
    assert set(restored) == {"santa"}  # a house that was missing has nothing to restore
    assert restored["santa"] == original


def test_restoring_without_the_owner_tier_keeps_the_calendar():
    original = snapshot()
    published = Snapshot(generated_at=NOW, properties=[original])
    restored = properties_from_payloads(published.family_payload(), None)["santa"]
    assert restored.stays == original.stays
    assert restored.kpis == [] and restored.reservations == [] and restored.kpi_columns == ()


def test_house_hours_travel_with_the_calendar():
    late = snapshot(check_in_time="16:30", check_out_time="10:00")
    published = Snapshot(generated_at=NOW, properties=[late])
    house = published.family_payload()["properties"][0]
    assert (house["check_in"], house["check_out"]) == ("16:30", "10:00")
    restored = properties_from_payloads(published.family_payload(), published.owner_payload())["santa"]
    assert (restored.check_in_time, restored.check_out_time) == ("16:30", "10:00")
    # A page published before the hours existed falls back to the usual ones.
    del house["check_in"], house["check_out"]
    older = properties_from_payloads({**published.family_payload(), "properties": [house]}, None)["santa"]
    assert (older.check_in_time, older.check_out_time) == ("15:00", "11:00")


def test_pages_written_by_an_older_layout_are_not_restored():
    published = Snapshot(generated_at=NOW, properties=[snapshot()])
    family = {**published.family_payload(), "v": 1}
    assert properties_from_payloads(family, published.owner_payload()) == {}


def test_warnings_are_never_published():
    noisy = snapshot(warnings=["dettagli delle prenotazioni non letti"])
    published = Snapshot(generated_at=NOW, properties=[noisy])
    assert "non letti" not in repr(published.family_payload()) + repr(published.owner_payload())
    assert noisy == snapshot()  # and they do not make two snapshots differ


def test_a_missing_house_is_published_as_missing_not_as_empty():
    payload = Snapshot(generated_at=NOW, properties=[MissingProperty("vassena", "Vassena")]).family_payload()
    assert payload["properties"] == [{"id": "vassena", "label": "Vassena", "missing": True}]
