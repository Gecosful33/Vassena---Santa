"""The owner's workbook of running costs. Every workbook here is invented."""

from datetime import date

import pytest

from casecal.costs import (
    LABEL_LIMIT,
    MAX_LINES,
    MAX_SHEETS,
    NOTE_LIMIT,
    CostLine,
    CostsError,
    CostSheet,
    parse_workbook,
    sheets_from_payload,
)
from workbooks import workbook, year_rows

HOUSES = [("mare", "Casa Mare"), ("lago", "Casa Lago")]
YEAR = year_rows()


def changed(rows: list[list], row: int, values: list) -> list[list]:
    """A copy of ``rows`` with one row replaced."""
    return [values if index == row else list(existing) for index, existing in enumerate(rows)]


def one_sheet(rows: list[list], houses=HOUSES) -> tuple[CostSheet, list[str]]:
    sheets, warnings = parse_workbook(workbook({"2026": rows}), houses)
    assert len(sheets) == 1
    return sheets[0], warnings


def refusal(rows: list[list], houses=HOUSES) -> str:
    with pytest.raises(CostsError) as caught:
        parse_workbook(workbook({"2026": rows}), houses)
    return str(caught.value)


def test_the_costs_are_read_house_by_house_in_cents():
    sheet, warnings = one_sheet(YEAR)
    assert warnings == []
    assert sheet.year == 2026 and sheet.updated == date(2026, 9, 1)
    assert [line.label for line in sheet.lines] == ["TARI", "IMU", "Spese condominiali", "Concessione del pontile"]
    # Each amount goes to the house its column is headed with, whatever the order of the columns.
    assert sheet.lines[0].amounts == {"lago": 54000, "mare": 18000}
    assert sheet.lines[1].amounts == {"lago": 390050, "mare": 145000}
    assert sheet.total("mare") == 273000 and sheet.total("lago") == 569050
    assert sheet.total("altrove") == 0


def test_income_rows_and_the_sheets_own_totals_are_not_costs():
    sheet, _ = one_sheet(YEAR)
    labels = {line.label for line in sheet.lines}
    assert not labels & {"ADR", "Notti", "Ricavi netti", "ENTRATE", "SPESE", "Totale Spese", "Data Aggiornamento"}


def test_a_cost_that_does_not_apply_to_a_house_is_blank_not_zero():
    sheet, _ = one_sheet(YEAR)
    assert sheet.lines[2].amounts == {"lago": None, "mare": 110000}  # written "n.a."
    assert sheet.lines[3].amounts == {"lago": 125000, "mare": None}  # left empty
    for blank in ("N.A.", "n/a", "-", "–", "  "):
        again, _ = one_sheet(changed(YEAR, 11, ["Spese condominiali", blank, 1100, 1100]))
        assert again.lines[2].amounts["lago"] is None
    # A row that applies to no house says nothing and is left out.
    fewer, _ = one_sheet(changed(YEAR, 11, ["Spese condominiali", "n.a.", None, None]))
    assert [line.label for line in fewer.lines] == ["TARI", "IMU", "Concessione del pontile"]


def test_the_remark_beside_a_cost_is_the_first_text_right_of_the_total():
    sheet, _ = one_sheet(YEAR)
    assert [line.note for line in sheet.lines] == ["saldo a dicembre", "", "", "da rinnovare"]


def test_amounts_are_rounded_to_the_cent_as_a_person_would():
    rows = changed(YEAR, 10, ["IMU", 12.345, 1450.0000000001, None])
    sheet, _ = one_sheet(changed(rows, 13, ["Totale Spese"]))
    assert sheet.lines[1].amounts == {"lago": 1235, "mare": 145000}
    # A credit is a cost below zero.
    sheet, _ = one_sheet(changed(changed(YEAR, 9, ["Rimborso", -120.5, 0, None]), 13, ["Totale Spese"]))
    assert sheet.lines[0].amounts == {"lago": -12050, "mare": 0}


def test_a_column_belongs_to_the_house_its_heading_begins_with():
    houses = [("santa", "Santa"), ("vassena", "Vassena")]
    rows = changed(YEAR, 2, ["Voce", "Vassena", "Santa Margherita", "Totale"])
    sheet, warnings = one_sheet(rows, houses)
    assert warnings == [] and sheet.lines[0].amounts == {"vassena": 54000, "santa": 18000}
    # The other way round, and whatever the capitals and the spaces.
    rows = changed(YEAR, 2, ["Voce", " VASSENA ", "santa", "Totale"])
    sheet, _ = one_sheet(rows, [("santa", "Santa Margherita"), ("vassena", "Vassena")])
    assert sheet.lines[0].amounts == {"vassena": 54000, "santa": 18000}
    # A name that merely starts with the same letters is another house.
    rows = changed(YEAR, 2, ["Voce", "Vassena", "Santander", "Totale"])
    sheet, warnings = one_sheet(changed(rows, 13, ["Totale Spese"]), houses)
    assert set(sheet.lines[0].amounts) == {"vassena"}
    assert warnings == [
        "foglio 2026: una colonna non corrisponde a nessuna casa ed è ignorata",
        "foglio 2026: manca la colonna della casa Santa",
    ]


def test_a_heading_that_fits_two_houses_is_refused():
    message = refusal(changed(YEAR, 2, ["Voce", "Casa", "Casa Mare", "Totale"]))
    assert "più case" in message
    message = refusal(changed(YEAR, 2, ["Voce", "Casa Mare", "Casa Mare", "Totale"]))
    assert "più colonne" in message
    assert "nessuna colonna" in refusal(changed(YEAR, 2, ["Voce", "Altro", "Ancora", "Totale"]))


def test_the_sheets_own_total_is_a_check_on_the_reading():
    _, warnings = one_sheet(changed(YEAR, 13, ["Totale Spese", 5690.5, 2700, 8390.5]))
    assert warnings == ["foglio 2026: il totale delle spese scritto nel foglio non è la somma delle voci"]
    # A cent of rounding is not a difference, and a total left empty is not checked.
    assert one_sheet(changed(YEAR, 13, ["Totale Spese", 5690.5, 2730.004, None]))[1] == []
    assert one_sheet(changed(YEAR, 13, ["Totale Spese"]))[1] == []


def test_only_the_sheets_named_after_a_year_are_read_oldest_first():
    later = changed(YEAR, 9, ["TARI", 600, 200, 800])
    later = changed(later, 13, ["Totale Spese"])
    data = workbook({"Appunti": [["Voce", "qualunque cosa"]], "2027": later, " 2026 ": YEAR, "Riepilogo 2026": [[1]]})
    sheets, warnings = parse_workbook(data, HOUSES)
    assert [sheet.year for sheet in sheets] == [2026, 2027] and warnings == []
    assert sheets[1].lines[0].amounts == {"lago": 60000, "mare": 20000}
    with pytest.raises(CostsError, match="nessun foglio ha per nome un anno"):
        parse_workbook(workbook({"Spese": YEAR, "1999": YEAR}), HOUSES)
    too_many = {str(2020 + n): YEAR for n in range(MAX_SHEETS + 1)}
    with pytest.raises(CostsError, match=f"più di {MAX_SHEETS} fogli"):
        parse_workbook(workbook(too_many), HOUSES)


def test_the_date_of_the_last_revision_is_optional():
    sheet, _ = one_sheet(YEAR[:14])
    assert sheet.updated is None
    sheet, _ = one_sheet(YEAR[:15] + [["Data aggiornamento:"], ["ieri"]])
    assert sheet.updated is None  # words are not a date
    sheet, _ = one_sheet(YEAR[:15] + [[None, None, "DATA AGGIORNAMENTO"], [None, None, date(2026, 11, 20)]])
    assert sheet.updated == date(2026, 11, 20)


@pytest.mark.parametrize(
    "rows, message",
    [
        (changed(YEAR, 2, ["Descrizione", "Casa Lago", "Casa Mare", "Totale"]), "«Voce»"),
        (changed(YEAR, 8, ["Uscite"]), "«SPESE»"),
        (changed(YEAR, 13, ["Somma", 5690.5, 2730, 8420.5]), "«Totale Spese»"),
        ([YEAR[2], ["SPESE"], ["Totale Spese", 0, 0, 0]], "vuoto"),
        (changed(YEAR, 10, ["IMU", True, 1450, None]), "riga 11: un importo non è un numero"),
        (changed(YEAR, 10, ["IMU", 3.9e9, 1450, None]), "riga 11: un importo non è plausibile"),
    ],
)
def test_a_sheet_that_cannot_be_trusted_is_refused(rows, message):
    assert message in refusal(rows)
    assert refusal(rows).startswith("foglio 2026")


def test_a_refusal_names_the_place_and_never_the_content():
    # The log of the nightly job is public; the workbook is not.
    rows = changed(YEAR, 10, ["Mutuo della casa", "chiedere a Ermenegilda", 1450, None])
    message = refusal(rows)
    assert message == "foglio 2026, riga 11: un importo non è un numero"
    rows = changed(YEAR, 2, ["Voce", "Casa Lago", "Casa Mare", "Segreta", "Totale"])
    _, warnings = one_sheet(changed(rows, 13, ["Totale Spese"]))
    assert warnings == ["foglio 2026: una colonna non corrisponde a nessuna casa ed è ignorata"]


def test_a_formula_nobody_computed_is_not_read_as_nothing():
    # A workbook saved by a program, not by Excel, holds formulas without their result.
    message = refusal(changed(YEAR, 10, ["IMU", "=B10*7", 1450, None]))
    assert message == "foglio 2026, riga 11: una formula non ha il valore calcolato (apri il file con Excel e salvalo)"
    # In the sheet's own total it only means there is nothing to check against.
    sheet, warnings = one_sheet(changed(YEAR, 13, ["Totale Spese", "=SUM(B10:B13)", "=SUM(C10:C13)", None]))
    assert warnings == [] and sheet.total("lago") == 569050


def test_what_is_not_a_workbook_is_refused_without_a_trace():
    for junk in (b"", b"non sono un foglio di calcolo", b"PK\x03\x04" + b"\x00" * 40, b"\xd0\xcf\x11\xe0vecchio formato xls"):
        with pytest.raises(CostsError) as caught:
            parse_workbook(junk, HOUSES)
        assert str(caught.value) == "il file non è una cartella di lavoro Excel (.xlsx) leggibile"


def test_long_texts_are_cut_and_endless_lists_refused():
    rows = changed(YEAR, 9, ["T" * 300, 540, 180, 720, "n" * 900])
    sheet, _ = one_sheet(rows)
    assert len(sheet.lines[0].label) == LABEL_LIMIT and len(sheet.lines[0].note) == NOTE_LIMIT
    many = [YEAR[2], ["SPESE"]] + [[f"Voce {n}", 1, 1, 2] for n in range(MAX_LINES + 1)] + [["Totale Spese"]]
    assert f"più di {MAX_LINES} voci" in refusal(many)
    assert len(one_sheet(many[:-2] + [["Totale Spese"]])[0].lines) == MAX_LINES


def test_names_are_tidied_of_stray_spaces():
    sheet, _ = one_sheet(changed(YEAR, 9, ["  Bolletta   luce ", 540, 180, 720, "  da   rivedere "]))
    assert sheet.lines[0].label == "Bolletta luce" and sheet.lines[0].note == "da rivedere"


def test_sheets_travel_in_the_page_and_come_back_the_same():
    sheet, _ = one_sheet(YEAR)
    stored = sheet.to_dict()
    assert stored["year"] == 2026 and stored["updated"] == "2026-09-01"
    assert stored["lines"][2] == {"label": "Spese condominiali", "amounts": {"lago": None, "mare": 110000}, "note": ""}
    assert sheets_from_payload([stored]) == [sheet]
    undated = CostSheet(year=2027, lines=(CostLine("IMU", {"mare": 1}),))
    assert sheets_from_payload([undated.to_dict()]) == [undated]


@pytest.mark.parametrize(
    "stored",
    [
        None,
        [],
        "2026",
        [{"year": 2026}],
        [{"year": "duemila", "lines": []}],
        [{"year": 2026, "lines": [{"label": "IMU"}]}],
        [{"year": 2026, "lines": [{"label": "IMU", "amounts": {"mare": 1.5}}]}],
        [{"year": 2026, "lines": [{"label": "IMU", "amounts": {"mare": True}}]}],
        [{"year": 2026, "updated": "ieri", "lines": []}],
    ],
)
def test_costs_stored_in_an_unexpected_shape_are_dropped(stored):
    assert sheets_from_payload(stored) == []
