"""The family's own layer on top of the managers' calendars: who uses a house, and when.

These entries do not come from a portal. Family members write them in the page,
and they are kept as an append-only log of small encrypted events, one per
change. Replaying the log from the oldest event gives the current entries, so
two people editing at once cannot overwrite each other's work: the later event
simply wins for the entry it touches.

An entry is a diary note, not a booking. It does not close any date on the
manager's system: until the owner blocks those nights there, guests can still
book them. The page works out, for every entry, whether its nights are already
blocked on the portal, and says so.

Events are written by anyone holding the family passphrase and are stored where
anyone can read the ciphertext, so everything decoded here is treated as
untrusted input and validated field by field.
"""

from __future__ import annotations

import binascii
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

from cryptography.exceptions import InvalidTag

from .crypto import decrypt_json, encrypt_json

EVENT_AAD = "casecal/v1/log"
EVENT_PREFIX = "casecal:1:"
# Events are padded to this block so that the length of a comment, which is
# public, does not tell a short name from a long note.
EVENT_PAD = 256
MAX_ENTRIES = 2000

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
_PROP_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
WHO_LIMIT, NOTE_LIMIT, BY_LIMIT = 80, 200, 40
# Bounds that keep a slip of the finger (year 2207, a ten-year stay) out of the diary.
MAX_NIGHTS = 366
FIRST_DAY, LAST_DAY = date(2000, 1, 1), date(2100, 1, 1)


class FamilyError(ValueError):
    """An entry or event is not well formed."""


def _text(raw: Any, limit: int, *, required: bool = False) -> str:
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise FamilyError("text expected")
    text = _CONTROL_RE.sub(" ", raw).strip()
    if len(text) > limit or (required and not text):
        raise FamilyError("text missing or too long")
    return text


def _day(raw: Any) -> date:
    if not isinstance(raw, str):
        raise FamilyError("date expected")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise FamilyError("invalid date") from exc


@dataclass(frozen=True)
class FamilyEntry:
    """Someone in the family using a house from ``start`` to ``end`` (check-out day)."""

    id: str
    prop: str
    start: date
    end: date
    who: str
    note: str = ""
    by: str = ""  # who wrote or last changed the entry, as they named themselves
    at: str = ""  # when, by the clock of their device (informational only)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prop": self.prop,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "who": self.who,
            "note": self.note,
            "by": self.by,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FamilyEntry":
        if not isinstance(raw, Mapping):
            raise FamilyError("entry expected")
        entry_id, prop = raw.get("id"), raw.get("prop")
        if not isinstance(entry_id, str) or not _ID_RE.match(entry_id):
            raise FamilyError("invalid entry id")
        if not isinstance(prop, str) or not _PROP_RE.match(prop):
            raise FamilyError("invalid house id")
        start, end = _day(raw.get("start")), _day(raw.get("end"))
        if not end > start:
            raise FamilyError("an entry must end after it starts")
        if start < FIRST_DAY or end > LAST_DAY or (end - start).days > MAX_NIGHTS:
            raise FamilyError("dates out of range")
        stamp = raw.get("at")
        return cls(
            id=entry_id,
            prop=prop,
            start=start,
            end=end,
            who=_text(raw.get("who"), WHO_LIMIT, required=True),
            note=_text(raw.get("note"), NOTE_LIMIT),
            by=_text(raw.get("by"), BY_LIMIT),
            at=stamp if isinstance(stamp, str) and _STAMP_RE.match(stamp) else "",
        )


BLOCKED, PENDING, CONFLICT, UNKNOWN = "blocked", "pending", "conflict", "unknown"


def entry_status(entry: FamilyEntry, house: Any) -> str:
    """Where an entry stands against the manager's calendar.

    ``blocked``: every night is closed to sale, nobody can book over it.
    ``pending``: some nights are still for sale; the manager has to block them.
    ``conflict``: guests are booked on some of its nights.
    ``unknown``: it cannot be told, because the house was not read or the
    portal publishes nothing yet for some of the nights.

    ``house`` is the house's snapshot (``None`` or a missing house: unknown).
    The page works the same answer out for itself in ``web/logic.js``
    (``entryStatus``); both are held to the same cases in
    ``tests/fixtures/entry_status.json``.
    """
    if house is None or getattr(house, "missing", False):
        return UNKNOWN
    guests = on_sale = unread = False
    night = entry.start
    while night < entry.end:
        if not house.covered_from <= night < house.covered_to:
            unread = True
        else:
            kind = next((stay.kind for stay in house.stays if stay.start <= night < stay.end), None)
            if kind is None:
                on_sale = True
            elif kind != "closed":
                guests = True
        night += timedelta(days=1)
    return CONFLICT if guests else PENDING if on_sale else UNKNOWN if unread else BLOCKED


def fold(
    events: Iterable[Mapping[str, Any]], entries: Mapping[str, FamilyEntry] | None = None
) -> dict[str, FamilyEntry]:
    """Replay events, oldest first, over the given entries.

    ``set`` writes a whole entry, ``del`` removes one, ``snap`` replaces
    everything (it is written when the passphrase changes). A malformed event
    is skipped: one bad comment must not take the diary down.
    """
    state = dict(entries or {})
    for event in events:
        try:
            kind = event.get("t")
            if kind == "set":
                entry = FamilyEntry.from_dict(event)
                if entry.id in state or len(state) < MAX_ENTRIES:
                    state[entry.id] = entry
            elif kind == "del":
                state.pop(event["id"], None)
            elif kind == "snap":
                listed = [FamilyEntry.from_dict(raw) for raw in event["entries"][:MAX_ENTRIES]]
                state = {entry.id: entry for entry in listed}
        except (FamilyError, AttributeError, KeyError, TypeError):
            continue
    return state


def ordered(entries: Iterable[FamilyEntry]) -> list[FamilyEntry]:
    return sorted(entries, key=lambda entry: (entry.start, entry.prop, entry.id))


def snapshot_event(entries: Iterable[FamilyEntry]) -> dict[str, Any]:
    return {"t": "snap", "entries": [entry.to_dict() for entry in ordered(entries)]}


def encode_event(key: bytes, event: Mapping[str, Any]) -> str:
    blob = encrypt_json(key, dict(event), aad=EVENT_AAD, pad=EVENT_PAD)
    return f"{EVENT_PREFIX}{blob['iv']}:{blob['ct']}"


def is_event(body: str) -> bool:
    return isinstance(body, str) and body.startswith(EVENT_PREFIX)


def decode_event(key: bytes, body: str) -> dict[str, Any] | None:
    """The event carried by a log comment, or ``None`` if this key cannot open it."""
    if not is_event(body):
        return None
    parts = body[len(EVENT_PREFIX):].strip().split(":")
    if len(parts) != 2:
        return None
    try:
        event = decrypt_json(key, {"iv": parts[0], "ct": parts[1]}, aad=EVENT_AAD)
    except (InvalidTag, ValueError, binascii.Error):
        return None
    return event if isinstance(event, dict) else None
