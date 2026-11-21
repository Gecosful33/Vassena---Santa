"""The running costs of the houses, which the owner keeps in a spreadsheet.

The workbook has one sheet per year, named after it ("2026"). In a sheet:

* a header row starts with "Voce" and names the houses, up to a "Totale" column;
* a row that says "SPESE" opens the list of costs, one per row: a name, a yearly
  amount per house ("n.a." where it does not apply) and, to the right of the
  total, an optional remark;
* a row whose name begins with "Totale" closes the list.

Only that list is read. The income rows of the sheet are the owner's own copy of
what the portals report, and the page takes those from the portals every night.

The workbook is private. It is fetched at run time (see ``r2.py``), never stored
in the repository, and nothing of its content is printed, because the logs of
this job are public: a message names a sheet and a row number at most.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from typing import Any, Mapping, Sequence

from openpyxl import load_workbook

MAX_SHEETS, MAX_ROWS, MAX_COLUMNS, MAX_LINES = 12, 400, 40, 80
LABEL_LIMIT, NOTE_LIMIT = 80, 200
MAX_AMOUNT = 10_000_000  # euro a year for one line: anything beyond is a slip of the hand

_YEAR_RE = re.compile(r"^\s*(20\d\d)\s*$")
# What the owner writes where a cost does not apply to a house.
_NOT_APPLICABLE = {"", "n.a.", "n.a", "na", "n/a", "n.d.", "nd", "-", "–", "—"}


class CostsError(ValueError):
    """The workbook cannot be used. The message is safe for a public log."""


@dataclass(frozen=True)
class CostLine:
    label: str
    amounts: dict[str, int | None]  # house id -> cents a year; None = does not apply
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "amounts": dict(self.amounts), "note": self.note}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CostLine":
        amounts: dict[str, int | None] = {}
        for house, value in dict(raw["amounts"]).items():
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError("amount is not a whole number of cents")
            amounts[str(house)] = value
        return cls(label=str(raw["label"]), amounts=amounts, note=str(raw.get("note") or ""))


@dataclass(frozen=True)
class CostSheet:
    year: int
    lines: tuple[CostLine, ...]
    updated: date | None = None  # when the owner last revised the figures, if the sheet says

    def total(self, house: str) -> int:
        return sum(line.amounts.get(house) or 0 for line in self.lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "updated": self.updated.isoformat() if self.updated else None,
            "lines": [line.to_dict() for line in self.lines],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CostSheet":
        updated = raw.get("updated")
        return cls(
            year=int(raw["year"]),
            lines=tuple(CostLine.from_dict(line) for line in raw["lines"]),
            updated=date.fromisoformat(updated) if updated else None,
        )


def sheets_from_payload(raw: Any) -> list[CostSheet]:
    """The cost sheets a published page carries; nothing if they are not as expected."""
    try:
        return [CostSheet.from_dict(sheet) for sheet in raw or []]
    except (AttributeError, KeyError, TypeError, ValueError):
        return []


# --------------------------------------------------------------------------- reading the workbook


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _key(value: Any) -> str:
    return _text(value).casefold()


def _same_house(header: str, label: str) -> bool:
    """"Santa Margherita" is the house called "Santa": one name begins with the other, word for word."""
    one, other = _key(header), _key(label)
    return bool(one and other) and (one == other or one.startswith(other + " ") or other.startswith(one + " "))


def _cell(grid: list[tuple[Any, ...]], row: int, column: int) -> Any:
    return grid[row][column] if row < len(grid) and column < len(grid[row]) else None


def _grid(sheet: Any) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in sheet.iter_rows(max_row=MAX_ROWS, max_col=MAX_COLUMNS, values_only=True)]


def _cents(value: Any, formula: Any, where: str) -> int | None:
    """A yearly amount in cents; ``None`` where the cost does not apply to the house."""
    if isinstance(value, bool):
        raise CostsError(f"{where}: un importo non è un numero")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or abs(value) > MAX_AMOUNT:
            raise CostsError(f"{where}: un importo non è plausibile")
        return int((Decimal(str(value)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if value is None:
        if isinstance(formula, str) and formula.startswith("="):
            # Written by a program that did not compute it: Excel stores the result on saving.
            raise CostsError(f"{where}: una formula non ha il valore calcolato (apri il file con Excel e salvalo)")
        return None
    if isinstance(value, str) and _key(value) in _NOT_APPLICABLE:
        return None
    raise CostsError(f"{where}: un importo non è un numero")


def _read_sheet(
    year: int,
    grid: list[tuple[Any, ...]],
    formulas: list[tuple[Any, ...]],
    houses: Sequence[tuple[str, str]],
) -> tuple[CostSheet, list[str]]:
    name = f"foglio {year}"
    warnings: list[str] = []

    header = next(
        ((r, c) for r, row in enumerate(grid) for c, value in enumerate(row) if _key(value) == "voce"), None
    )
    if header is None:
        raise CostsError(f"{name}: non trovo la riga di intestazione che inizia con «Voce»")
    header_row, label_column = header

    # The houses' columns: from the one after "Voce" up to "Totale".
    columns: dict[str, int] = {}
    last_column = label_column
    unknown = 0
    for column in range(label_column + 1, len(grid[header_row])):
        title = _text(grid[header_row][column])
        if not title:
            continue
        if _key(title).startswith("totale"):
            last_column = column
            break
        last_column = column
        matching = [house for house, label in houses if _same_house(title, label)]
        if len(matching) > 1 or any(house in columns for house in matching):
            raise CostsError(f"{name}: una colonna corrisponde a più case, o una casa a più colonne")
        if matching:
            columns[matching[0]] = column
        else:
            unknown += 1
    if not columns:
        raise CostsError(f"{name}: nessuna colonna porta il nome di una casa")
    if unknown:
        warnings.append(f"{name}: una colonna non corrisponde a nessuna casa ed è ignorata")
    for house, label in houses:
        if house not in columns:
            warnings.append(f"{name}: manca la colonna della casa {label}")

    opening = next(
        (r for r in range(header_row + 1, len(grid)) if _key(_cell(grid, r, label_column)) == "spese"), None
    )
    if opening is None:
        raise CostsError(f"{name}: non trovo la riga «SPESE» che apre l'elenco delle spese")

    lines: list[CostLine] = []
    closing = None
    for row in range(opening + 1, len(grid)):
        label = _text(_cell(grid, row, label_column))
        if not label:
            continue
        if _key(label).startswith("totale"):
            closing = row
            break
        where = f"{name}, riga {row + 1}"
        amounts = {
            house: _cents(_cell(grid, row, column), _cell(formulas, row, column), where)
            for house, column in columns.items()
        }
        if all(amount is None for amount in amounts.values()):
            continue  # a cost that applies to no house says nothing
        note = next(
            (_text(value) for value in grid[row][last_column + 1:] if _text(value)), ""
        )
        lines.append(CostLine(label=label[:LABEL_LIMIT], amounts=amounts, note=note[:NOTE_LIMIT]))
        if len(lines) > MAX_LINES:
            raise CostsError(f"{name}: più di {MAX_LINES} voci di spesa")
    if closing is None:
        raise CostsError(f"{name}: manca la riga «Totale Spese» che chiude l'elenco")
    if not lines:
        raise CostsError(f"{name}: l'elenco delle spese è vuoto")

    # The sheet's own total is a check on the reading, never the figure that is shown.
    for house, column in columns.items():
        written = _cell(grid, closing, column)
        if isinstance(written, (int, float)) and not isinstance(written, bool) and math.isfinite(written):
            listed = sum(line.amounts[house] or 0 for line in lines)
            if abs(round(written * 100) - listed) > 1:
                warnings.append(f"{name}: il totale delle spese scritto nel foglio non è la somma delle voci")
                break

    updated = None
    for r, row in enumerate(grid):
        for c, value in enumerate(row):
            if _key(value).startswith("data aggiornamento"):
                below = _cell(grid, r + 1, c)
                if isinstance(below, datetime):
                    updated = below.date()
                elif isinstance(below, date):
                    updated = below
    return CostSheet(year=year, lines=tuple(lines), updated=updated), warnings


def parse_workbook(data: bytes, houses: Sequence[tuple[str, str]]) -> tuple[list[CostSheet], list[str]]:
    """The cost sheets of an .xlsx workbook, oldest year first, and what was odd about them.

    ``houses`` are ``(id, label)`` pairs: a column belongs to the house whose label
    its heading begins with. Raises ``CostsError`` when a sheet cannot be trusted.
    """
    unreadable = "il file non è una cartella di lavoro Excel (.xlsx) leggibile"
    try:
        values = load_workbook(BytesIO(data), read_only=True, data_only=True)
        formulas = load_workbook(BytesIO(data), read_only=True, data_only=False)
    except Exception as exc:  # noqa: BLE001 - whatever the library makes of a file that is not a workbook
        raise CostsError(unreadable) from exc
    try:
        years: dict[int, Any] = {}
        for sheet in values.worksheets:
            match = _YEAR_RE.match(sheet.title or "")
            if match and int(match.group(1)) not in years:
                years[int(match.group(1))] = sheet
        if not years:
            raise CostsError("nessun foglio ha per nome un anno (per esempio «2026»)")
        if len(years) > MAX_SHEETS:
            raise CostsError(f"più di {MAX_SHEETS} fogli con il nome di un anno")
        sheets: list[CostSheet] = []
        warnings: list[str] = []
        for year in sorted(years):
            try:  # the rows are parsed only now, as they are read
                grid, formula_grid = _grid(years[year]), _grid(formulas[years[year].title])
            except Exception as exc:  # noqa: BLE001
                raise CostsError(f"foglio {year}: {unreadable}") from exc
            sheet, notes = _read_sheet(year, grid, formula_grid, houses)
            sheets.append(sheet)
            warnings.extend(notes)
        return sheets, warnings
    finally:
        values.close()
        formulas.close()
