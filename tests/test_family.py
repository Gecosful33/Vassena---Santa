from datetime import date

import pytest

from casecal.crypto import derive_key, site_salt
from casecal.family import (
    EVENT_PREFIX,
    FamilyEntry,
    FamilyError,
    decode_event,
    encode_event,
    fold,
    is_event,
    ordered,
    snapshot_event,
)

KEY = derive_key("nebbia-quercia-sentiero-42", site_salt("test"), 100_000)
OTHER = derive_key("lanterna-golfo-ardesia-17", site_salt("test"), 100_000)


def raw(**overrides):
    entry = {
        "id": "e_k3j9x2m1q8",
        "prop": "santa",
        "start": "2027-07-03",
        "end": "2027-07-10",
        "who": "Marco e Giulia",
        "note": "arriviamo sabato sera",
        "by": "Marco",
        "at": "2026-11-20T12:30:00Z",
    }
    entry.update(overrides)
    return entry


def test_an_entry_round_trips():
    entry = FamilyEntry.from_dict(raw())
    assert (entry.start, entry.end) == (date(2027, 7, 3), date(2027, 7, 10))
    assert entry.to_dict() == raw()
    assert FamilyEntry.from_dict(entry.to_dict()) == entry


def test_text_is_tidied_not_trusted():
    entry = FamilyEntry.from_dict(raw(who="  Nonna\tAda \n", note=None, by=None, at="ieri"))
    assert entry.who == "Nonna Ada"  # control characters become spaces, ends are trimmed
    assert (entry.note, entry.by, entry.at) == ("", "", "")  # an unreadable timestamp is dropped


@pytest.mark.parametrize(
    "change",
    [
        {"id": "short"},
        {"id": "has spaces in it"},
        {"id": 12345678},
        {"prop": "Santa"},
        {"prop": "../etc"},
        {"start": "03/07/2027"},
        {"start": 20270703},
        {"end": "2027-07-03"},  # no night at all
        {"end": "2028-07-05"},  # longer than a year
        {"start": "1999-12-30", "end": "2000-01-02"},  # outside the calendar's century...
        {"start": "2099-12-30", "end": "2100-01-02"},  # ...on either side
        {"end": "2027-07-01"},
        {"who": ""},
        {"who": "   "},
        {"who": "x" * 81},
        {"who": ["Marco"]},
        {"note": "x" * 201},
        {"by": "x" * 41},
    ],
)
def test_malformed_entries_are_refused(change):
    with pytest.raises(FamilyError):
        FamilyEntry.from_dict(raw(**change))
    with pytest.raises(FamilyError):
        FamilyEntry.from_dict(["not", "a", "mapping"])


def test_the_log_replays_into_the_current_entries():
    events = [
        {"t": "set", **raw()},
        {"t": "set", **raw(id="e_second0001", prop="vassena", who="Nonna")},
        {"t": "set", **raw(end="2027-07-12", by="Giulia")},  # the first entry, edited later
        {"t": "del", "id": "e_second0001", "by": "Nonna"},
    ]
    state = fold(events)
    assert set(state) == {"e_k3j9x2m1q8"}
    assert state["e_k3j9x2m1q8"].end == date(2027, 7, 12)
    assert state["e_k3j9x2m1q8"].by == "Giulia"


def test_a_deleted_entry_can_be_written_again():
    events = [{"t": "set", **raw()}, {"t": "del", "id": raw()["id"]}, {"t": "set", **raw(who="Paolo")}]
    assert fold(events)[raw()["id"]].who == "Paolo"


def test_replay_continues_from_entries_already_known():
    known = fold([{"t": "set", **raw()}])
    later = fold([{"t": "set", **raw(id="e_second0001", who="Nonna")}], known)
    assert set(later) == {"e_k3j9x2m1q8", "e_second0001"}
    assert set(known) == {"e_k3j9x2m1q8"}  # the input is left alone


def test_bad_events_are_skipped_without_losing_the_rest():
    events = [
        {"t": "set", **raw()},
        {"t": "set", **raw(id="e_broken0001", end="2020-01-01")},
        {"t": "del"},  # no id
        {"t": "wipe"},  # unknown kind
        "not an event",
        None,
        {"t": "snap", "entries": "nope"},
        {"t": "set", **raw(id="e_second0001", who="Nonna")},
    ]
    assert set(fold(events)) == {"e_k3j9x2m1q8", "e_second0001"}


def test_a_snapshot_replaces_everything_before_it():
    kept = FamilyEntry.from_dict(raw(id="e_kept000001", who="Paolo"))
    events = [
        {"t": "set", **raw()},
        snapshot_event([kept]),
        {"t": "set", **raw(id="e_after00001", who="Nonna")},
    ]
    assert set(fold(events)) == {"e_kept000001", "e_after00001"}


def test_the_diary_cannot_be_flooded():
    from casecal import family

    flood = [{"t": "set", **raw(id=f"e_{n:010d}")} for n in range(family.MAX_ENTRIES + 50)]
    state = fold(flood)
    assert len(state) == family.MAX_ENTRIES
    # An entry that already exists can still be edited when the diary is full.
    edited = fold([{"t": "set", **raw(id="e_0000000000", who="Paolo")}], state)
    assert edited["e_0000000000"].who == "Paolo" and len(edited) == family.MAX_ENTRIES


def test_entries_are_listed_by_date():
    later = FamilyEntry.from_dict(raw(id="e_later00001", start="2027-08-01", end="2027-08-05"))
    sooner = FamilyEntry.from_dict(raw(id="e_sooner0001", start="2027-06-01", end="2027-06-05"))
    assert [e.id for e in ordered([later, sooner])] == ["e_sooner0001", "e_later00001"]


def test_events_are_encrypted_and_only_the_right_key_opens_them():
    body = encode_event(KEY, {"t": "set", **raw()})
    assert is_event(body) and body.startswith(EVENT_PREFIX)
    for secret in ("Marco", "santa", "2027", "arriviamo"):
        assert secret not in body
    assert decode_event(KEY, body) == {"t": "set", **raw()}
    assert decode_event(OTHER, body) is None


def test_event_length_does_not_follow_the_text():
    short = encode_event(KEY, {"t": "set", **raw(who="Ada", note="")})
    longer = encode_event(KEY, {"t": "set", **raw(who="Marco, Giulia e i bambini", note="con il cane")})
    assert len(short) == len(longer)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "ciao a tutti",
        EVENT_PREFIX,
        EVENT_PREFIX + "onlyonepart",
        EVENT_PREFIX + "a:b:c",
        EVENT_PREFIX + "not base64!:also not",
        EVENT_PREFIX + "AAAA:AAAA",
    ],
)
def test_comments_that_are_not_ours_decode_to_nothing(body):
    assert decode_event(KEY, body) is None


def test_a_tampered_event_is_rejected():
    body = encode_event(KEY, {"t": "del", "id": "e_k3j9x2m1q8"})
    flipped = body[:-6] + ("A" if body[-6] != "A" else "B") + body[-5:]
    assert decode_event(KEY, flipped) is None
