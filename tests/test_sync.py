from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from casecal import sync
from casecal.config import Config, PortalCredentials, PropertyConfig
from casecal.crypto import open_envelope
from casecal.errors import LayoutError, LoginError, SyncAborted
from casecal.model import MissingProperty, MonthKpi, PropertySnapshot, Reservation, Snapshot, Stay
from casecal.site import extract_envelope
from casecal.sync import collect, describe_error, previous_from_site, publish, target_years

NOW = datetime(2026, 8, 15, 2, 17, tzinfo=timezone.utc)
FAMILY = "nebbia-quercia-sentiero-42"
OWNER = "lanterna-golfo-ardesia-17"

CONFIG = Config(
    title="Calendario case",
    site_id="test/site",
    properties=(
        PropertyConfig(id="santa", label="Santa", env="SANTA"),
        PropertyConfig(id="vassena", label="Vassena", env="VASSENA"),
    ),
)
CREDENTIALS = {
    "santa": PortalCredentials("https://portal.example/a", "user-a", "pass-a"),
    "vassena": PortalCredentials("https://portal.example/b", "user-b", "pass-b"),
}


def house(prop_id: str, fetched_at: datetime = NOW) -> PropertySnapshot:
    return PropertySnapshot(
        id=prop_id,
        label=prop_id.title(),
        fetched_at=fetched_at,
        covered_from=date(2026, 1, 1),
        covered_to=date(2028, 1, 1),
        stays=[Stay(date(2026, 8, 1), date(2026, 8, 7), channel="Airbnb", guests=4)],
        kpi_columns=("ADR", "Imposta", "Netto proprietario"),
        kpi_roles={"adr": 0, "net": 2},
        kpis=[MonthKpi(2026, 8, 6, (Decimal("400.00"), Decimal("504.00"), Decimal("1100.00")))],
        reservation_columns=("Totale pernottamento", "Netto proprietario"),
        reservation_roles={"gross": 0, "net": 1},
        reservations=[Reservation(date(2026, 8, 1), date(2026, 8, 7), "Airbnb", 4, (Decimal("2400.00"), Decimal("1100.00")))],
    )


def fetcher(failing: dict[str, Exception] | None = None):
    failing = failing or {}
    calls = []

    def fetch(prop, credentials, years, now):
        calls.append((prop.id, credentials.username, tuple(years)))
        if prop.id in failing:
            raise failing[prop.id]
        return house(prop.id, now)

    fetch.calls = calls
    return fetch


def no_previous():
    raise AssertionError("the published page must not be fetched when every portal answered")


def test_all_portals_read():
    fetch = fetcher()
    snapshot, report = collect(CONFIG, CREDENTIALS, fetch=fetch, load_previous=no_previous, now=NOW)
    assert report.fresh == ["santa", "vassena"]
    assert not report.degraded and not report.errors
    assert [p.id for p in snapshot.properties] == ["santa", "vassena"]
    assert fetch.calls == [("santa", "user-a", (2026, 2027)), ("vassena", "user-b", (2026, 2027))]


def test_one_failure_reuses_the_published_data_for_that_house_only():
    yesterday = NOW - timedelta(days=1)
    old = house("vassena", yesterday)
    old.label = "Old label"
    snapshot, report = collect(
        CONFIG,
        CREDENTIALS,
        fetch=fetcher({"vassena": LoginError("credenziali rifiutate")}),
        load_previous=lambda: {"vassena": old, "santa": house("santa", yesterday)},
        now=NOW,
    )
    assert report.fresh == ["santa"] and report.reused == ["vassena"] and report.missing == []
    assert report.degraded
    assert report.errors == {"vassena": "LoginError: credenziali rifiutate"}
    santa, vassena = snapshot.properties
    assert santa.fetched_at == NOW  # the healthy house is fresh, not the stored copy
    assert vassena.fetched_at == yesterday  # the page can tell how old this one is
    assert vassena.label == "Vassena"  # the current name wins over the stored one
    assert snapshot.generated_at == NOW


def test_a_failure_without_earlier_data_is_published_as_missing():
    snapshot, report = collect(
        CONFIG,
        CREDENTIALS,
        fetch=fetcher({"santa": LayoutError("tabella non trovata")}),
        load_previous=lambda: {},
        now=NOW,
    )
    assert report.missing == ["santa"] and report.reused == []
    assert isinstance(snapshot.properties[0], MissingProperty)
    assert snapshot.family_payload()["properties"][0] == {"id": "santa", "label": "Santa", "missing": True}


def test_a_house_read_with_warnings_is_published_but_flags_the_run():
    def fetch(prop, credentials, years, now):
        snapshot = house(prop.id, now)
        if prop.id == "vassena":
            snapshot.warnings.append("dettagli delle prenotazioni non letti (TransportError: HTTP 503)")
        return snapshot

    snapshot, report = collect(CONFIG, CREDENTIALS, fetch=fetch, load_previous=no_previous, now=NOW)
    assert report.fresh == ["santa", "vassena"] and not report.errors
    assert report.warnings == {"vassena": ["dettagli delle prenotazioni non letti (TransportError: HTTP 503)"]}
    assert report.degraded  # the calendar is fresh, yet someone must look at the portal


def test_nothing_is_published_when_every_portal_fails():
    with pytest.raises(SyncAborted):
        collect(
            CONFIG,
            CREDENTIALS,
            fetch=fetcher({"santa": LoginError("x"), "vassena": LoginError("y")}),
            load_previous=no_previous,
            now=NOW,
        )


def test_unexpected_errors_are_logged_without_their_message():
    def explode():
        raise ValueError("could not parse amount 'EUR 6.584,04 Mario Rossi'")

    try:
        explode()
    except ValueError as exc:
        line = describe_error(exc)
    assert line.startswith("ValueError")
    assert "6.584" not in line and "Mario" not in line
    assert describe_error(LoginError("credenziali rifiutate")) == "LoginError: credenziali rifiutate"


def test_years_follow_the_houses_time_zone():
    assert target_years(CONFIG, NOW) == [2026, 2027]
    new_year_in_rome = datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc)
    assert target_years(CONFIG, new_year_in_rome) == [2027, 2028]
    assert target_years(Config(years_ahead=0, properties=CONFIG.properties), NOW) == [2026]


def test_publish_writes_a_page_both_passphrases_open(tmp_path):
    snapshot = Snapshot(generated_at=NOW, properties=[house("santa"), house("vassena")])
    index = publish(
        snapshot, family_passphrase=FAMILY, owner_passphrase=OWNER, site_id="test/site", out_dir=tmp_path
    )
    envelope = extract_envelope(index.read_text(encoding="utf-8"))
    family, owner = open_envelope(envelope, family_passphrase=FAMILY)
    assert family == snapshot.family_payload() and owner is None
    family, owner = open_envelope(envelope, owner_passphrase=OWNER)
    assert owner["properties"] == snapshot.owner_payload()["properties"]


def test_the_salt_is_stable_across_nightly_builds(tmp_path):
    snapshot = Snapshot(generated_at=NOW, properties=[house("santa")])
    salts = []
    for night in ("a", "b"):
        index = publish(
            snapshot, family_passphrase=FAMILY, owner_passphrase=OWNER, site_id="test/site", out_dir=tmp_path / night
        )
        salts.append(extract_envelope(index.read_text(encoding="utf-8"))["kdf"]["salt"])
    assert salts[0] == salts[1]  # otherwise remembered keys would break every morning


def _published_envelope(tmp_path, snapshot):
    index = publish(
        snapshot, family_passphrase=FAMILY, owner_passphrase=OWNER, site_id="test/site", out_dir=tmp_path
    )
    return extract_envelope(index.read_text(encoding="utf-8"))


def test_previous_data_is_recovered_from_the_published_page(tmp_path, monkeypatch):
    original = house("vassena", NOW - timedelta(days=1))
    envelope = _published_envelope(tmp_path, Snapshot(generated_at=NOW, properties=[original]))
    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: envelope)
    restored = previous_from_site("https://example.github.io/repo", family_passphrase=FAMILY, owner_passphrase=OWNER)
    assert restored == {"vassena": original}


def test_previous_data_is_ignored_when_it_cannot_be_trusted(tmp_path, monkeypatch):
    real = _published_envelope(tmp_path / "real", Snapshot(generated_at=NOW, properties=[house("santa")]))
    demo = _published_envelope(
        tmp_path / "demo", Snapshot(generated_at=NOW, properties=[house("santa")], demo=True)
    )
    args = dict(family_passphrase=FAMILY, owner_passphrase=OWNER)

    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: None)
    assert previous_from_site("https://x.example", **args) == {}  # nothing online yet
    assert previous_from_site(None, **args) == {}  # no site address configured

    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: demo)
    assert previous_from_site("https://x.example", **args) == {}  # invented data never feeds a real page

    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: real)
    wrong = dict(family_passphrase="x" * 20, owner_passphrase="y" * 20)
    assert previous_from_site("https://x.example", **wrong) == {}  # passphrases were rotated

    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: {"v": 99})
    assert previous_from_site("https://x.example", **args) == {}  # garbage
