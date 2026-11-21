"""Invented workbooks of running costs, in the layout the owner keeps theirs."""

from datetime import datetime
from io import BytesIO

from openpyxl import Workbook


def year_rows(first: str = "Casa Lago", second: str = "Casa Mare") -> list[list]:
    """One year's sheet: income first (the owner's own copy of the portals' figures,
    which the page does not read), then the costs, then the date of the last revision."""
    return [
        [None, "Analisi entrate e spese"],
        [],
        ["Voce", first, second, "Totale"],
        ["ENTRATE"],
        ["ADR", 250, 300, None],
        ["Notti", 40, 30, 70],
        ["Ricavi netti", 6000, 5000, 11000],
        [],
        ["SPESE"],
        ["TARI", 540, 180, 720, "saldo a dicembre"],
        ["IMU", 3900.5, 1450, 5350.5],
        ["Spese condominiali", "n.a.", 1100, 1100],
        ["Concessione del pontile", 1250, None, 1250, None, "da rinnovare"],
        ["Totale Spese", 5690.5, 2730, 8420.5],
        [],
        ["Data Aggiornamento"],
        [datetime(2026, 9, 1)],
    ]


def workbook(sheets: dict[str, list[list]]) -> bytes:
    """An .xlsx file with these sheets, as bytes."""
    book = Workbook()
    book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()
