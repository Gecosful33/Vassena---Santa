from datetime import date
from decimal import Decimal

import pytest

from casecal.errors import LayoutError
from casecal.model import BOOKING, CLOSED, Reservation, Stay
from casecal.parse import (
    attach_details,
    cross_check,
    is_logged_in,
    is_login_page,
    parse_login_form,
    parse_reservations,
    parse_units,
    parse_year_grid,
    parse_year_kpis,
    stays_from_grids,
)
from portal_html import eur, grid_classes, landing_page, login_page, reservations_page, stats_page


def d(text: str) -> date:
    return date.fromisoformat(text)


STAYS = [
    Stay(d("2026-05-30"), d("2026-06-02")),  # crosses a month
    Stay(d("2026-06-08"), d("2026-06-10")),
    Stay(d("2026-07-01"), d("2026-08-01"), CLOSED),  # a whole month blocked
    Stay(d("2026-08-01"), d("2026-08-07")),  # turnover on 7 August...
    Stay(d("2026-08-07"), d("2026-08-10")),  # ...into the next booking
    Stay(d("2026-09-12"), d("2026-09-13")),  # a single night
    Stay(d("2026-09-20"), d("2026-09-21"), CLOSED),  # a single blocked night
    Stay(d("2026-12-31"), d("2027-01-03")),  # crosses New Year
    Stay(d("2027-03-25"), d("2028-01-01"), CLOSED),  # blocked to the end of the data
]


def cells_for(year: int) -> dict[date, str]:
    cells = {night: klass for night, klass in grid_classes(STAYS).items() if night.year == year}
    # The portal puts no end marker on a block that continues past the year shown.
    if year == 2027:
        cells[d("2027-12-31")] = "b"
    return cells


def nights_by_month(year: int) -> dict[int, int]:
    counts: dict[int, int] = {}
    for night, klass in cells_for(year).items():
        if "b" not in klass.split():
            counts[night.month] = counts.get(night.month, 0) + 1
    return counts


def page(year: int, **overrides) -> str:
    kpi_rows = {
        month: (count, [Decimal("200.00"), Decimal("42.00") * count, Decimal("100.10") * count])
        for month, count in nights_by_month(year).items()
    }
    return stats_page(year, **{"cells": cells_for(year), "kpi_rows": kpi_rows, **overrides})


# ----------------------------------------------------------------------------- grid


def test_grid_reads_bookings_blocks_and_their_markers():
    grid = parse_year_grid(page(2026), 2026, "7")
    assert grid.nights[d("2026-05-30")] == BOOKING and grid.nights[d("2026-06-01")] == BOOKING
    assert d("2026-06-02") not in grid.nights  # check-out day: that night is free
    assert grid.nights[d("2026-07-15")] == CLOSED
    assert {d("2026-05-30"), d("2026-08-01"), d("2026-08-07"), d("2026-07-01")} <= grid.starts
    assert {d("2026-06-01"), d("2026-08-06"), d("2026-08-09"), d("2026-07-31")} <= grid.ends
    assert d("2026-09-12") in grid.starts and d("2026-09-12") in grid.ends  # one night: both markers
    assert grid.booking_nights(5) == 2 and grid.booking_nights(6) == 3 and grid.booking_nights(7) == 0
    assert grid.booking_nights(8) == 9


def test_yearly_grids_join_into_stays_across_new_year():
    grids = [parse_year_grid(page(year), year, "7") for year in (2027, 2026)]  # order must not matter
    assert stays_from_grids(grids) == STAYS


def test_a_booking_running_past_the_data_needs_no_marker_on_that_side():
    cells = {d("2026-01-01"): "s", d("2026-01-02"): "d", d("2026-12-30"): "a", d("2026-12-31"): "s"}
    grid = parse_year_grid(stats_page(2026, cells=cells), 2026, "7")
    assert stays_from_grids([grid]) == [
        Stay(d("2026-01-01"), d("2026-01-03")),
        Stay(d("2026-12-30"), d("2027-01-01")),
    ]


@pytest.mark.parametrize(
    "cells, message",
    [
        ({d("2026-03-10"): "s", d("2026-03-11"): "d"}, "arrivo"),
        ({d("2026-03-10"): "a", d("2026-03-11"): "s"}, "partenza"),
    ],
)
def test_a_booking_without_its_markers_is_refused(cells, message):
    grid = parse_year_grid(stats_page(2026, cells=cells), 2026, "7")
    with pytest.raises(LayoutError, match=message):
        stays_from_grids([grid])


def test_grids_must_be_consecutive_years():
    grids = [parse_year_grid(stats_page(year), year, "7") for year in (2026, 2028)]
    with pytest.raises(LayoutError, match="consecutive"):
        stays_from_grids(grids)
    with pytest.raises(LayoutError):
        stays_from_grids([])


def test_leap_years_are_anchored_to_the_calendar():
    leap = parse_year_grid(stats_page(2028, cells={d("2028-02-29"): "a d"}), 2028, "7")
    assert leap.nights == {d("2028-02-29"): BOOKING}
    # A 2027 grid presented as 2028 has 29 February disabled: it cannot be the year it claims.
    wrong = stats_page(2027, selected_year=2028)
    with pytest.raises(LayoutError, match="calendario dell'anno"):
        parse_year_grid(wrong, 2028, "7")


def test_the_page_must_show_the_year_and_unit_asked_for():
    with pytest.raises(LayoutError, match="anno richiesto"):
        parse_year_grid(stats_page(2026, selected_year=2025), 2026, "7")
    with pytest.raises(LayoutError, match="appartamento richiesto"):
        parse_year_grid(stats_page(2026, other_units=(("9", "Altra"),), selected_unit="9"), 2026, "7")
    with pytest.raises(LayoutError, match="anno richiesto"):
        parse_year_kpis(stats_page(2026, selected_year=2025), 2026, "7")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda html: html.replace('class="owncal"', 'class="calendar"'), "non trovata"),
        (lambda html: html.replace("<tr><th>Dic</th>", "<tr hidden><th>Dic</th>", 1).replace(
            "<tr hidden><th>Dic</th>" + '<td class=""></td>' * 31 + "</tr>", "", 1), "dodici mesi"),
        (lambda html: html.replace('<tr><th>Gen</th><td class=""></td>', "<tr><th>Gen</th>", 1), "31 celle"),
        (lambda html: html.replace('<td class=""></td>', '<td class="opt"></td>', 1), "non prevista"),
        (lambda html: html.replace('<td class=""></td>', '<td class="s b"></td>', 1), "sia prenotata sia bloccata"),
        (lambda html: html.replace('<td class=""></td>', '<td class="ab"></td>', 1), "marcatore di blocco"),
        (lambda html: html.replace('<td class=""></td>', '<td class="dis"></td>', 1), "calendario dell'anno"),
    ],
)
def test_a_changed_grid_is_refused_rather_than_guessed(mutate, message):
    html = mutate(stats_page(2026))
    with pytest.raises(LayoutError, match=message):
        parse_year_grid(html, 2026, "7")


# ----------------------------------------------------------------------------- statistics


def test_statistics_with_the_first_manager_layout():
    kpis = parse_year_kpis(page(2026), 2026, "7")
    assert kpis.columns == ("ADR", "Imposta", "Netto proprietario")
    assert kpis.roles == {"adr": 0, "net": 2}
    august = kpis.months[7]
    assert (august.year, august.month, august.room_nights, august.occupancy_pct) == (2026, 8, 9, 29)
    assert august.values == (Decimal("200.00"), Decimal("378.00"), Decimal("900.90"))
    january = kpis.months[0]
    assert january.room_nights == 0 and january.values == (None, None, None)
    assert len(kpis.months) == 12


def test_the_totals_row_is_kept_as_the_portal_states_it():
    rows = {month: (1, ["100", "21", "33.33"]) for month in range(1, 13)}
    html = stats_page(2026, kpi_rows=rows, totals=[12, "3%", eur("100"), eur("252"), eur("400.01")])
    total = parse_year_kpis(html, 2026, "7").total
    assert (total.year, total.room_nights, total.occupancy_pct) == (2026, 12, 3)
    # 12 x 33,33 = 399,96 but the portal says 400,01: the portal's figure is what gets shown.
    assert total.values == (Decimal("100.00"), Decimal("252.00"), Decimal("400.01"))
    empty = parse_year_kpis(stats_page(2025, totals=[0, "0%", "", "", ""]), 2025, "7").total
    assert empty.room_nights == 0 and empty.values == (None, None, None)
    without = stats_page(2026).replace("<tfoot>", "<!--").replace("</tfoot>", "-->")
    assert parse_year_kpis(without, 2026, "7").total is None


def test_statistics_with_a_manager_who_publishes_more_columns():
    columns = ("ADR", "PM", "Proprietario", "Imposta", "Addebiti al proprietario", "Netto proprietario")
    rows = {5: (15, ["688.77", "1995.58", "6183.02", "1298.44", "0", "4884.59"])}
    kpis = parse_year_kpis(stats_page(2026, kpi_columns=columns, kpi_rows=rows), 2026, "7")
    assert kpis.columns == columns
    assert kpis.roles == {"adr": 0, "net": 5}
    assert kpis.months[4].values[5] == Decimal("4884.59")
    assert kpis.months[4].values[4] == Decimal("0.00")


def test_statistics_without_recognisable_roles_still_parse():
    kpis = parse_year_kpis(stats_page(2026, kpi_columns=("Incasso",), kpi_rows={3: (2, ["10"])}), 2026, "7")
    assert kpis.columns == ("Incasso",) and kpis.roles == {}


def test_monthly_rounding_may_drift_from_the_total_by_cents_only():
    rows = {month: (1, ["100", "21", "33.33"]) for month in range(1, 13)}
    close = stats_page(2026, kpi_rows=rows, totals=[12, "3%", eur("100"), eur("252"), eur("400.01")])
    assert len(parse_year_kpis(close, 2026, "7").months) == 12
    far = stats_page(2026, kpi_rows=rows, totals=[12, "3%", eur("100"), eur("252"), eur("410.00")])
    with pytest.raises(LayoutError, match="netto proprietario non torna"):
        parse_year_kpis(far, 2026, "7")
    nights = stats_page(2026, kpi_rows=rows, totals=[13, "3%", eur("100"), eur("252"), eur("399.96")])
    with pytest.raises(LayoutError, match="room nights non torna"):
        parse_year_kpis(nights, 2026, "7")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda html: html.replace('class="ownstcal"', 'class="stats"'), "non trovata"),
        (lambda html: html.replace("Room nights", "Notti vendute"), "Room nights"),
        (lambda html: html.replace('<td class="text-center">0</td>', '<td class="text-center">n/d</td>', 1), "non numeriche"),
        (lambda html: html.replace("€200,00", "200 euro", 1), "importo non riconosciuto"),
        (lambda html: html.replace('<td class="text-center">0%</td>', '<td class="text-center">0%</td><td></td>', 1), "numero di celle"),
        (lambda html: html.replace("<th>Dic</th><td class=\"text-center\">", "<th>Dic</th><!--", 1).replace(
            "</tr></tbody><tfoot>", "--></tr></tbody><tfoot>", 1), "numero di celle"),
    ],
)
def test_changed_statistics_are_refused(mutate, message):
    with pytest.raises(LayoutError, match=message):
        parse_year_kpis(mutate(page(2026)), 2026, "7")


def test_grid_and_statistics_must_agree_on_nights_sold():
    grid = parse_year_grid(page(2026), 2026, "7")
    cross_check(grid, parse_year_kpis(page(2026), 2026, "7"))  # consistent: passes
    off_by_one = stats_page(2026, cells=cells_for(2026), kpi_rows={8: (8, ["200", "336", "800.80"])})
    with pytest.raises(LayoutError, match="non coincidono"):
        cross_check(grid, parse_year_kpis(off_by_one, 2026, "7"))
    with pytest.raises(LayoutError, match="anni diversi"):
        cross_check(grid, parse_year_kpis(page(2027), 2027, "7"))


# ----------------------------------------------------------------------------- reservations

FIRST = ["Appartamento", "Nome", "Nazione", "Canale", "Ospiti", "Check in", "Check out", "Notti",
         "Totale pernottamento", "Imposta", "Commissioni PM", "Commissioni OTA", "Netto proprietario", ""]
SECOND = ["Appartamento", "Nome", "Canale", "Check in", "Check out", "Data prenotazione",
          "Totale pernottamento", "Importo totale (Comm. OTA Incl)", "Commissioni OTA", "Tot proprietario", ""]
RANGE = dict(start=d("2026-01-01"), end=d("2027-12-31"))


def first_layout() -> str:
    return reservations_page(RANGE["start"], RANGE["end"], FIRST, [
        ["Casa Demo", "Mario Rossi", "IT", "Airbnb", "4", "30/05/2026", "02/06/2026", "3",
         "€750,00", "€157,50", "€120,00", "€112,50", "€360,00", ""],
        ["Casa Demo", "Greta Müller", "DE", "Booking Engine", "2", "08/06/2026", "10/06/2026", "2",
         "€1.250,50", "€262,61", "€200,08", "€0,00", "€787,81", ""],
    ])


def test_reservations_with_the_first_manager_layout():
    table = parse_reservations(first_layout(), **RANGE)
    assert table.columns == ("Totale pernottamento", "Imposta", "Commissioni PM", "Commissioni OTA", "Netto proprietario")
    assert table.roles == {"gross": 0, "net": 4}
    assert table.rows == [
        Reservation(d("2026-05-30"), d("2026-06-02"), "Airbnb", 4,
                    (Decimal("750.00"), Decimal("157.50"), Decimal("120.00"), Decimal("112.50"), Decimal("360.00"))),
        Reservation(d("2026-06-08"), d("2026-06-10"), "Booking Engine", 2,
                    (Decimal("1250.50"), Decimal("262.61"), Decimal("200.08"), Decimal("0.00"), Decimal("787.81"))),
    ]


def test_guest_names_and_nationalities_never_leave_the_parser():
    table = parse_reservations(first_layout(), **RANGE)
    dumped = repr(table)
    for personal in ("Mario", "Rossi", "Greta", "Müller", "'IT'", "'DE'", "Nome", "Nazione"):
        assert personal not in dumped


def test_reservations_with_the_second_manager_layout():
    html = reservations_page(RANGE["start"], RANGE["end"], SECOND, [
        ["Casa Demo", "1234/2026", "Diretta", "01/05/2026", "03/05/2026", "12/03/2026 18:04:11",
         "€900,00", "€1.000,00", "€100,00", "€650,00", ""],
    ])
    table = parse_reservations(html, **RANGE)
    assert table.columns == ("Totale pernottamento", "Importo totale (Comm. OTA Incl)", "Commissioni OTA", "Tot proprietario")
    assert table.roles == {"gross": 0, "net": 3}
    assert table.rows[0].guests is None and table.rows[0].channel == "Diretta"
    assert "1234/2026" not in repr(table) and "18:04" not in repr(table)


def test_an_empty_list_has_no_rows_and_no_amount_columns():
    table = parse_reservations(reservations_page(RANGE["start"], RANGE["end"], FIRST, []), **RANGE)
    assert table.rows == [] and table.columns == ()


def test_negative_and_missing_amounts():
    html = reservations_page(RANGE["start"], RANGE["end"], ["Check in", "Check out", "Storno", "Extra"], [
        ["01/05/2026", "03/05/2026", "€-12,00", ""],
        ["05/05/2026", "06/05/2026", "-€3,50", "€5,00"],
    ])
    table = parse_reservations(html, **RANGE)
    assert table.columns == ("Storno", "Extra")
    assert [row.values for row in table.rows] == [(Decimal("-12.00"), None), (Decimal("-3.50"), Decimal("5.00"))]


def test_a_free_text_column_is_never_taken_for_an_amount():
    html = reservations_page(RANGE["start"], RANGE["end"], ["Check in", "Check out", "Note", "Totale"], [
        ["01/05/2026", "03/05/2026", "€100,00", "€100,00"],
        ["05/05/2026", "06/05/2026", "chiamare la signora Bianchi", "€50,00"],
    ])
    table = parse_reservations(html, **RANGE)
    assert table.columns == ("Totale",)
    assert "Bianchi" not in repr(table)


@pytest.mark.parametrize(
    "html, message",
    [
        (reservations_page(RANGE["start"], RANGE["end"], FIRST, [], shown_to="31/12/2026"), "intervallo richiesto"),
        (reservations_page(RANGE["start"], RANGE["end"], ["Arrivo", "Check out"], []), "Check in / Check out"),
        (reservations_page(RANGE["start"], RANGE["end"], ["Check in", "Check out"], [["01/05/2026"]]), "numero di celle"),
        (reservations_page(RANGE["start"], RANGE["end"], ["Check in", "Check out"], [["1 maggio", "03/05/2026"]]), "data non riconosciuta"),
        (reservations_page(RANGE["start"], RANGE["end"], ["Check in", "Check out"], [["31/02/2026", "03/05/2026"]]), "data non valida"),
        (reservations_page(RANGE["start"], RANGE["end"], FIRST, []).replace("table-res", "table-x"), "non trovato"),
    ],
)
def test_changed_reservation_lists_are_refused(html, message):
    with pytest.raises(LayoutError, match=message):
        parse_reservations(html, **RANGE)


def test_details_are_attached_to_the_matching_booking():
    stays = [Stay(d("2026-05-30"), d("2026-06-02")), Stay(d("2026-07-01"), d("2026-08-01"), CLOSED)]
    reservations = [Reservation(d("2026-05-30"), d("2026-06-02"), "Airbnb", 4)]
    detailed, unmatched = attach_details(stays, reservations, covered_from=d("2026-01-01"), covered_to=d("2028-01-01"))
    assert unmatched == 0
    assert (detailed[0].channel, detailed[0].guests) == ("Airbnb", 4)
    assert detailed[1] == stays[1]  # blocks carry no details


def test_mismatches_are_counted_and_the_calendar_wins():
    stays = [Stay(d("2026-05-30"), d("2026-06-02")), Stay(d("2026-06-08"), d("2026-06-10"))]
    reservations = [
        Reservation(d("2026-05-30"), d("2026-06-02"), "Airbnb", 4),
        Reservation(d("2026-09-01"), d("2026-09-04"), "Booking", 2),  # not in the calendar
    ]
    detailed, unmatched = attach_details(stays, reservations, covered_from=d("2026-01-01"), covered_to=d("2028-01-01"))
    assert unmatched == 2  # one booking without reservation, one reservation without booking
    assert [(s.start, s.end) for s in detailed] == [(s.start, s.end) for s in stays]
    assert detailed[1].channel is None


def test_reservations_are_clipped_to_the_years_read_before_matching():
    stays = [Stay(d("2026-01-01"), d("2026-01-03")), Stay(d("2027-12-30"), d("2028-01-01"))]
    reservations = [
        Reservation(d("2025-12-29"), d("2026-01-03"), "Airbnb", 2),  # began before the first year
        Reservation(d("2027-12-30"), d("2028-01-04"), "Diretta", 5),  # ends after the last one
        Reservation(d("2025-12-20"), d("2025-12-27"), "Booking", 2),  # entirely outside: ignored
    ]
    detailed, unmatched = attach_details(stays, reservations, covered_from=d("2026-01-01"), covered_to=d("2028-01-01"))
    assert unmatched == 0
    assert [s.channel for s in detailed] == ["Airbnb", "Diretta"]


# ----------------------------------------------------------------------------- login and units


def test_login_form_keeps_the_session_token_and_fixed_fields():
    form = parse_login_form(login_page("gestore"))
    assert form.action == "/gestore/login/"
    assert form.fields == {"f00dfeed01[f00dfeed01]": "c0ffee", "property_id": "gestore", "username": "", "password": ""}
    assert form.submit_name == "login_form"


def test_login_form_is_found_without_its_class_and_refused_when_unrecognisable():
    assert parse_login_form(login_page().replace('class="login-form"', "")).action == "/demo/login/"
    with pytest.raises(LayoutError, match="non trovato"):
        parse_login_form("<html><body><p>Manutenzione</p></body></html>")
    with pytest.raises(LayoutError, match="non riconosciuti"):
        parse_login_form(login_page().replace('name="username"', 'name="user"'))


def test_login_state_is_recognised():
    assert is_login_page(login_page()) and not is_logged_in(login_page())
    assert is_logged_in(landing_page()) and not is_login_page(landing_page())


def test_units_come_from_the_apartment_selector():
    html = stats_page(2026, unit_id="41", unit_name="Bilocale Mare", other_units=(("9", "Attico"),))
    assert [(u.id, u.name) for u in parse_units(html)] == [("41", "Bilocale Mare"), ("9", "Attico")]
    with pytest.raises(LayoutError, match="selettore"):
        parse_units(landing_page())
    with pytest.raises(LayoutError, match="nessun appartamento"):
        parse_units('<select name="apt"><option value="">[Tutti]</option></select>')
