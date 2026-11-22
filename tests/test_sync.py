import base64
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import unquote

import pytest

from casecal import r2, sync
from casecal.config import Config, PortalCredentials, PropertyConfig
from casecal.costs import CostLine, CostSheet
from casecal.crypto import AAD_OWNER, derive_key, encrypt_json, open_envelope
from casecal.errors import LayoutError, LoginError, SyncAborted
from casecal.model import MissingProperty, MonthKpi, PropertySnapshot, Snapshot, Stay
from casecal.site import extract_envelope
from casecal.photos import MAX_SOURCE_BYTES, Photo, prepare
from casecal.sync import (
    CostsState,
    PhotosState,
    PreviousPage,
    collect,
    costs_state,
    describe_error,
    photos_state,
    previous_from_site,
    publish,
    target_years,
)
from pictures import picture, upright_picture
from workbooks import workbook, year_rows

NOW = datetime(2026, 8, 15, 2, 17, tzinfo=timezone.utc)
FAMILY = "nebbia-quercia-sentiero-42"  # the page's one passphrase
OWNER = "lanterna-golfo-ardesia-17"    # what opened the figures before the single access

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
        load_previous=lambda: PreviousPage(properties={"vassena": old, "santa": house("santa", yesterday)}),
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


def test_reused_data_takes_the_house_hours_from_the_current_configuration():
    config = Config(properties=(
        CONFIG.properties[0],
        PropertyConfig(id="vassena", label="Vassena", env="VASSENA", check_in="16:00", check_out="10:00"),
    ))
    old = house("vassena", NOW - timedelta(days=1))  # stored with the usual 15:00 / 11:00
    snapshot, _ = collect(
        config,
        CREDENTIALS,
        fetch=fetcher({"vassena": LoginError("credenziali rifiutate")}),
        load_previous=lambda: PreviousPage(properties={"vassena": old}),
        now=NOW,
    )
    reused = snapshot.properties[1]
    assert (reused.check_in_time, reused.check_out_time) == ("16:00", "10:00")


def test_a_failure_without_earlier_data_is_published_as_missing():
    snapshot, report = collect(
        CONFIG,
        CREDENTIALS,
        fetch=fetcher({"santa": LayoutError("tabella non trovata")}),
        load_previous=PreviousPage,
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
        raise ValueError("could not parse amount 'EUR 1.234,56 Mario Rossi'")

    try:
        explode()
    except ValueError as exc:
        line = describe_error(exc)
    assert line.startswith("ValueError")
    assert "1.234" not in line and "Mario" not in line
    assert describe_error(LoginError("credenziali rifiutate")) == "LoginError: credenziali rifiutate"


def test_years_follow_the_houses_time_zone():
    assert target_years(CONFIG, NOW) == [2026, 2027]
    new_year_in_rome = datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc)
    assert target_years(CONFIG, new_year_in_rome) == [2027, 2028]
    assert target_years(Config(years_ahead=0, properties=CONFIG.properties), NOW) == [2026]


def test_publish_writes_a_page_its_one_passphrase_opens(tmp_path):
    snapshot = Snapshot(generated_at=NOW, properties=[house("santa"), house("vassena")])
    index = publish(snapshot, passphrase=FAMILY, site_id="test/site", out_dir=tmp_path)
    envelope = extract_envelope(index.read_text(encoding="utf-8"))
    family, owner = open_envelope(envelope, passphrase=FAMILY)
    assert family == snapshot.family_payload()
    assert owner == snapshot.owner_payload()  # the figures open with the same passphrase
    assert open_envelope(envelope, passphrase=OWNER) == (None, None)


def test_the_salt_is_stable_across_nightly_builds(tmp_path):
    snapshot = Snapshot(generated_at=NOW, properties=[house("santa")])
    salts = []
    for night in ("a", "b"):
        index = publish(snapshot, passphrase=FAMILY, site_id="test/site", out_dir=tmp_path / night)
        salts.append(extract_envelope(index.read_text(encoding="utf-8"))["kdf"]["salt"])
    assert salts[0] == salts[1]  # otherwise remembered keys would break every morning


def _published_envelope(tmp_path, snapshot):
    index = publish(snapshot, passphrase=FAMILY, site_id="test/site", out_dir=tmp_path)
    return extract_envelope(index.read_text(encoding="utf-8"))


def _as_published_with_two_passphrases(envelope, snapshot):
    """The same page as the job wrote it when the figures had a passphrase of their own."""
    salt = base64.b64decode(envelope["kdf"]["salt"])
    family_key = derive_key(FAMILY, salt, envelope["kdf"]["iterations"])
    figures = {**snapshot.owner_payload(), "family_key": base64.b64encode(family_key).decode()}
    owner_key = derive_key(OWNER, salt, envelope["kdf"]["iterations"])
    return {**envelope, "owner": encrypt_json(owner_key, figures, aad=AAD_OWNER)}


def test_previous_data_is_recovered_from_the_published_page(tmp_path, monkeypatch):
    original = house("vassena", NOW - timedelta(days=1))
    envelope = _published_envelope(tmp_path, Snapshot(generated_at=NOW, properties=[original]))
    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: envelope)
    restored = previous_from_site("https://example.github.io/repo", passphrase=FAMILY)
    assert restored.properties == {"vassena": original}
    assert restored.entries == [] and restored.log is None


def test_a_page_published_with_two_passphrases_is_read_back_whole_with_the_old_one(tmp_path, monkeypatch):
    # What the job finds online the first night after the figures lost their own passphrase.
    original = house("vassena", NOW - timedelta(days=1))
    snapshot = Snapshot(generated_at=NOW, properties=[original])
    old_page = _as_published_with_two_passphrases(_published_envelope(tmp_path, snapshot), snapshot)
    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: old_page)
    whole = previous_from_site("https://x.example", passphrase=FAMILY, legacy_owner_passphrase=OWNER)
    assert whole.properties == {"vassena": original}
    # Without the old passphrase the calendar is still recovered; only the figures are not.
    partial = previous_from_site("https://x.example", passphrase=FAMILY)
    assert partial.properties["vassena"].stays == original.stays
    assert partial.properties["vassena"] != original


def test_previous_data_is_ignored_when_it_cannot_be_trusted(tmp_path, monkeypatch):
    real = _published_envelope(tmp_path / "real", Snapshot(generated_at=NOW, properties=[house("santa")]))
    demo = _published_envelope(
        tmp_path / "demo", Snapshot(generated_at=NOW, properties=[house("santa")], demo=True)
    )
    args = dict(passphrase=FAMILY)

    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: None)
    assert previous_from_site("https://x.example", **args) == PreviousPage()  # nothing online yet
    assert previous_from_site(None, **args) == PreviousPage()  # no site address configured

    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: demo)
    assert previous_from_site("https://x.example", **args) == PreviousPage()  # invented data never feeds a real page

    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: real)
    wrong = dict(passphrase="x" * 20, legacy_owner_passphrase="y" * 20)
    assert previous_from_site("https://x.example", **wrong) == PreviousPage()  # the passphrase was changed

    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: {"v": 99})
    assert previous_from_site("https://x.example", **args) == PreviousPage()  # garbage


# --------------------------------------------------------------------------- the running costs

COSTS = CostSheet(
    year=2026,
    lines=(CostLine("IMU", {"santa": 145000, "vassena": 390050}, "stima"), CostLine("Giardiniere", {"santa": None, "vassena": 240000})),
    updated=date(2026, 9, 1),
)
ACCOUNT = "0123456789abcdef0123456789abcdef"  # an Account ID: 32 hexadecimal characters
STORE = f"https://{ACCOUNT}.r2.cloudflarestorage.com"
BUCKET = dict(url=f"{STORE}/case-private/spese.xlsx", key_id="key-id-0123", secret="key-secret-4567")


def never_fetched(url, key_id, secret):
    raise AssertionError("the bucket must not be contacted in this scenario")


def published_with_costs() -> PreviousPage:
    return PreviousPage(costs=[COSTS])


def test_without_its_settings_there_is_no_profit_and_loss_and_nothing_to_report():
    state = costs_state(CONFIG, url=None, key_id=None, secret=None, load_previous=no_previous, fetch=never_fetched)
    assert state == CostsState([], [], fresh=False)


def test_the_workbook_is_fetched_and_read_for_the_houses_of_the_calendar():
    asked = []

    def fetch(url, key_id, secret):
        asked.append((url, key_id, secret))
        # The owner heads the columns with the houses' full names.
        return workbook({"2026": year_rows("Vassena", "Santa Margherita")})

    state = costs_state(CONFIG, **BUCKET, load_previous=no_previous, fetch=fetch)
    assert asked == [(BUCKET["url"], BUCKET["key_id"], BUCKET["secret"])]
    assert state.fresh and state.warnings == []
    assert [sheet.year for sheet in state.sheets] == [2026]
    assert state.sheets[0].lines[0].amounts == {"vassena": 54000, "santa": 18000}
    assert state.sheets[0].updated == date(2026, 9, 1)


def test_an_oddity_in_the_workbook_flags_the_run_and_its_costs_are_used_all_the_same():
    rows = year_rows("Vassena", "Santa")
    rows[13] = ["Totale Spese", 1, 1, 2]  # the sheet's own total has drifted from its lines
    state = costs_state(CONFIG, **BUCKET, load_previous=no_previous, fetch=lambda *_: workbook({"2026": rows}))
    assert state.fresh and state.sheets[0].total("santa") == 273000
    assert state.warnings == ["foglio 2026: il totale delle spese scritto nel foglio non è la somma delle voci"]


@pytest.mark.parametrize("missing", ["url", "key_id", "secret"])
def test_an_incomplete_setting_keeps_the_costs_already_published(missing):
    settings = {**BUCKET, missing: None}
    state = costs_state(CONFIG, **settings, load_previous=published_with_costs, fetch=never_fetched)
    assert state.sheets == [COSTS] and not state.fresh
    assert len(state.warnings) == 1
    for name in ("COSTS_R2_URL", "COSTS_R2_ACCESS_KEY_ID", "COSTS_R2_SECRET_ACCESS_KEY"):
        assert name in state.warnings[0]
    for value in BUCKET.values():
        assert value not in state.warnings[0]


def test_a_workbook_that_cannot_be_fetched_or_read_keeps_the_costs_already_published():
    def refused(url, key_id, secret):
        raise r2.StorageError("file non trovato: controlla il nome del bucket e del file in COSTS_R2_URL")

    def exploding(url, key_id, secret):
        raise RuntimeError(f"unexpected, quoting {url} and {secret}")

    for fetch, reason in (
        (refused, "file non trovato: controlla il nome del bucket e del file in COSTS_R2_URL"),
        (lambda *_: b"not a workbook", "il file non è una cartella di lavoro Excel (.xlsx) leggibile"),
        (lambda *_: workbook({"Spese": year_rows()}), "nessun foglio ha per nome un anno (per esempio «2026»)"),
    ):
        state = costs_state(CONFIG, **BUCKET, load_previous=published_with_costs, fetch=fetch)
        assert state.sheets == [COSTS] and not state.fresh
        assert state.warnings == [f"spese non lette ({reason}): restano quelle già pubblicate"]

    # Whatever else goes wrong must not stop the calendar, nor reach the public log in full.
    state = costs_state(CONFIG, **BUCKET, load_previous=published_with_costs, fetch=exploding)
    assert state.sheets == [COSTS] and not state.fresh
    assert state.warnings[0].startswith("spese non lette (RuntimeError (sync.py:")
    for value in BUCKET.values():
        assert value not in state.warnings[0]

    # With nothing published yet there are no costs to keep: the page has no profit and loss.
    state = costs_state(CONFIG, **BUCKET, load_previous=PreviousPage, fetch=refused)
    assert state.sheets == [] and len(state.warnings) == 1


def test_the_address_of_the_bucket_alone_is_enough_to_find_the_workbook():
    # What Cloudflare shows, ready to be copied, among the settings of a bucket: no file name to get wrong.
    folder = f"{STORE}/case-private"
    book = workbook({"2026": year_rows("Vassena", "Santa")})
    asked = []

    def fetch(url, key_id, secret):
        asked.append(url)
        return book

    def holding(*files):
        return lambda url, key_id, secret: list(files)

    for address in (folder, folder + "/"):
        asked.clear()
        state = costs_state(CONFIG, **{**BUCKET, "url": address}, load_previous=no_previous, fetch=fetch,
                            listing=holding("IMG_0001.jpeg", "Analisi Entrate-Spese.XLSX", "~$Analisi Entrate-Spese.XLSX", "appunti.txt"))
        assert state.fresh and state.sheets[0].total("santa") == 273000
        assert asked == [f"{folder}/Analisi%20Entrate-Spese.XLSX"]  # the one Excel file there, whatever it is called

    # Nothing to choose, or too much: the costs already published stay, and the reason names no file.
    for files, reason in (
        (("IMG_0001.jpeg", "appunti.txt"), "nel bucket non c'è nessun file Excel (.xlsx)"),
        ((), "nel bucket non c'è nessun file Excel (.xlsx)"),
        (("spese 2026.xlsx", "spese vecchie.xlsx"),
         "nel bucket ci sono più file Excel (.xlsx): indica quale, aggiungendo /<file> a COSTS_R2_URL"),
    ):
        state = costs_state(CONFIG, **{**BUCKET, "url": folder}, load_previous=published_with_costs,
                            fetch=never_fetched, listing=holding(*files))
        assert state.sheets == [COSTS] and not state.fresh
        assert state.warnings == [f"spese non lette ({reason}): restano quelle già pubblicate"]

    # The bucket cannot be listed: said as it is.
    def refused(url, key_id, secret):
        raise r2.StorageError("chiave rifiutata, oppure senza il permesso di lettura su questo bucket")

    state = costs_state(CONFIG, **{**BUCKET, "url": folder}, load_previous=published_with_costs, fetch=never_fetched, listing=refused)
    assert state.sheets == [COSTS] and "chiave rifiutata" in state.warnings[0]

    # An address that names the workbook is fetched as before, without looking at what else is there.
    def never_listed(url, key_id, secret):
        raise AssertionError("the bucket must not be listed when the address names the file")

    asked.clear()
    state = costs_state(CONFIG, **BUCKET, load_previous=no_previous, fetch=fetch, listing=never_listed)
    assert state.fresh and asked == [BUCKET["url"]]


def test_the_account_alone_is_enough_when_the_configuration_names_the_bucket():
    # What its owner can copy from Cloudflare without composing anything: the Account ID, the
    # endpoint shown beside the keys, or the address of the bucket's page in the dashboard.
    named = replace(CONFIG, bucket="case-private")
    folder = f"{STORE}/case-private"
    book = workbook({"2026": year_rows("Vassena", "Santa")})
    for pasted in (ACCOUNT, f"  {ACCOUNT.upper()}\n", STORE, f"{STORE}/",
                   f"https://dash.cloudflare.com/{ACCOUNT}/r2/default/buckets/case-private/settings",
                   f"https://dash.cloudflare.com/{ACCOUNT}/r2/overview"):
        listed, asked = [], []

        def listing(url, key_id, secret):
            listed.append(url)
            return ["IMG_0001.jpeg", "Spese case.xlsx"]

        def fetch(url, key_id, secret):
            asked.append(url)
            return book

        state = costs_state(named, **{**BUCKET, "url": pasted}, load_previous=no_previous, fetch=fetch, listing=listing)
        assert state.fresh and state.warnings == []
        assert listed == [folder] and asked == [f"{folder}/Spese%20case.xlsx"]
        # The photos are looked for in the same place.
        held = bucket({"santa.jpg": picture(), "vassena.png": picture((600, 400), "PNG")})
        found = photos_state(named, **{**BUCKET, "url": pasted}, load_previous=no_previous, fetch=held, listing=held.listing)
        assert found.fresh == ["santa", "vassena"] and found.warnings == []
        assert [url for url, *_ in held.listed] == [folder] and {url.rsplit("/", 1)[0] for url, *_ in held.asked} == {folder}

    # Without a bucket in the configuration the account alone does not say where to look.
    state = costs_state(CONFIG, **{**BUCKET, "url": ACCOUNT}, load_previous=published_with_costs, fetch=never_fetched, listing=never_fetched)
    assert state.sheets == [COSTS] and "COSTS_R2_URL non valido: manca il nome del bucket" in state.warnings[0]


def test_an_address_left_with_its_blanks_is_said_to_be_so_for_costs_and_photos_alike():
    named = replace(CONFIG, bucket="case-private")
    pasted = "https://<ACCOUNT_ID>.r2.cloudflarestorage.com/case-private"  # the line of the instructions, as it was
    reason = f"COSTS_R2_URL non valido: contiene ancora un segnaposto, come <ACCOUNT_ID>: al suo posto va il valore vero. {r2.EXPECTED}"
    state = costs_state(named, **{**BUCKET, "url": pasted}, load_previous=published_with_costs, fetch=never_fetched, listing=never_fetched)
    assert state.sheets == [COSTS] and not state.fresh
    assert state.warnings == [f"spese non lette ({reason}): restano quelle già pubblicate"]
    found = photos_state(named, **{**BUCKET, "url": pasted}, load_previous=published_with_photos,
                         fetch=never_fetched, listing=never_fetched)
    assert found.photos == ONLINE and found.fresh == [] and found.absent == []
    assert found.warnings == [f"foto non lette ({reason}): santa, vassena; restano quelle già pubblicate"]


def test_the_bucket_is_read_through_the_signed_fetch_unless_told_otherwise(monkeypatch):
    asked = []
    monkeypatch.setattr(r2, "fetch", lambda *settings: asked.append(settings) or workbook({"2026": year_rows("Vassena", "Santa")}))
    state = costs_state(CONFIG, **BUCKET, load_previous=no_previous)
    assert state.fresh and asked == [(BUCKET["url"], BUCKET["key_id"], BUCKET["secret"])]


def test_the_costs_travel_with_the_figures_and_are_recovered_from_the_published_page(tmp_path, monkeypatch):
    snapshot = Snapshot(generated_at=NOW, properties=[house("santa"), house("vassena")], costs=[COSTS])
    envelope = _published_envelope(tmp_path, snapshot)
    family, figures = open_envelope(envelope, passphrase=FAMILY)
    assert figures["costs"] == [COSTS.to_dict()]
    assert "costs" not in family and "IMU" not in str(family)  # nothing of them with the calendar
    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: envelope)
    assert previous_from_site("https://x.example", passphrase=FAMILY).costs == [COSTS]
    # A page published before the costs existed simply has none.
    older = _published_envelope(tmp_path / "older", Snapshot(generated_at=NOW, properties=[house("santa")]))
    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: older)
    assert previous_from_site("https://x.example", passphrase=FAMILY).costs == []


# --------------------------------------------------------------------------- the houses' photos

ONLINE = {"santa": Photo(b"the picture of santa already online"), "vassena": Photo(b"the picture of vassena already online")}
FOLDER = f"{STORE}/case-private"


def published_with_photos() -> PreviousPage:
    return PreviousPage(photos=dict(ONLINE))


def bucket(files, written=None):
    """A stand-in for the bucket: ``files`` maps a file name to its bytes, or to the error asking for it raises.

    ``written`` says when the files it names were last uploaded, as the listing shows it.
    """
    asked, listed = [], []

    def fetch(url, key_id, secret, *, max_bytes):
        asked.append((url, key_id, secret, max_bytes))
        held = files.get(unquote(url.rsplit("/", 1)[1]), r2.NotFound("file non trovato"))
        if isinstance(held, Exception):
            raise held
        return held

    def listing(url, key_id, secret):
        listed.append((url, key_id, secret))
        return [
            r2.Entry(name, len(held) if isinstance(held, bytes) else 1234, (written or {}).get(name, "2026-11-22T08:00:00.000Z"))
            for name, held in files.items()
        ]

    fetch.asked, fetch.listing, fetch.listed = asked, listing, listed
    return fetch


def photos(config, fetch, *, previous=no_previous, **place):
    """What the job makes of the photos in the bucket ``fetch`` stands in for."""
    return photos_state(config, **{**BUCKET, **place}, load_previous=previous, fetch=fetch, listing=fetch.listing)


def fetched(fetch):
    """The names of the files that were asked for, in order."""
    return [unquote(url.rsplit("/", 1)[1]) for url, *_ in fetch.asked]


def test_without_the_buckets_settings_there_are_no_photos_and_nothing_to_report():
    state = photos_state(CONFIG, url=None, key_id=None, secret=None, load_previous=no_previous,
                         fetch=never_fetched, listing=never_fetched)
    assert state == PhotosState({}, [], [])


def test_each_house_has_the_photo_kept_under_its_name_beside_the_workbook():
    fetch = bucket({"spese.xlsx": b"PK...", "santa.jpg": picture((1600, 1200)), "vassena.jpg": picture((1200, 1600), "PNG")})
    state = photos(CONFIG, fetch)
    # The folder the workbook is in is listed once; then only the files that are the houses' photos are fetched.
    assert fetch.listed == [(BUCKET["url"], BUCKET["key_id"], BUCKET["secret"])]
    assert fetch.asked == [
        (f"{FOLDER}/santa.jpg", BUCKET["key_id"], BUCKET["secret"], MAX_SOURCE_BYTES),
        (f"{FOLDER}/vassena.jpg", BUCKET["key_id"], BUCKET["secret"], MAX_SOURCE_BYTES),
    ]
    assert state.fresh == ["santa", "vassena"] and state.warnings == [] and state.stray == 0
    assert state.photos == {"santa": prepare(picture((1600, 1200))), "vassena": prepare(picture((1200, 1600), "PNG"))}
    assert all(photo.mime == "image/webp" for photo in state.photos.values())


def test_a_house_without_a_photo_simply_has_none():
    fetch = bucket({"vassena.jpg": picture()})
    # Even if one was online: the owner took it away. No fault, so no warning; only a note of which house.
    state = photos(CONFIG, fetch)
    assert set(state.photos) == {"vassena"} and state.fresh == ["vassena"] and state.warnings == []
    assert state.absent == ["santa"]
    nothing = photos(CONFIG, bucket({"spese.xlsx": b"PK...", "appunti.txt": b"..."}))
    assert nothing == PhotosState({}, [], [], ["santa", "vassena"])


def test_a_photo_is_found_under_any_of_the_endings_a_photo_is_saved_with():
    # A phone says ".jpeg" as often as ".jpg"; a screenshot is a ".png".
    fetch = bucket({"santa.png": picture((300, 200), "PNG"), "santa.jpeg": picture((900, 600)), "vassena.webp": picture((600, 400), "WEBP")})
    state = photos(CONFIG, fetch)
    assert fetched(fetch) == ["santa.jpeg", "vassena.webp"]  # of two with the house's name, the usual ending first
    assert state.photos == {"santa": prepare(picture((900, 600))), "vassena": prepare(picture((600, 400), "WEBP"))}
    assert state.fresh == ["santa", "vassena"] and state.absent == [] and state.warnings == []


def test_a_photo_is_known_by_the_house_its_name_begins_with():
    # However the name is capitalised, and whatever follows the house's: a phone that saves a
    # file a second time calls it "santa 2.jpg".
    for name in ("Santa.jpg", "SANTA.JPEG", "santa 2.jpg", "santa-cucina.png", "Santa (1).webp", "santa_nuova.jpg"):
        fetch = bucket({name: picture((900, 600), "PNG" if name.endswith("png") else "WEBP" if name.endswith("webp") else "JPEG")})
        state = photos(CONFIG, fetch)
        assert fetched(fetch) == [name] and state.fresh == ["santa"] and state.absent == ["vassena"], name
    # The house's own name comes before those, and of those the last one uploaded comes first.
    fetch = bucket(
        {"santa 2.jpg": picture((300, 200)), "santa.jpg": picture((900, 600)), "santa 3.jpg": picture((600, 400)),
         "vassena 2.jpg": picture((300, 200)), "vassena 3.jpg": picture((600, 400))},
        written={"santa.jpg": "2026-11-01T08:00:00.000Z", "santa 2.jpg": "2026-11-21T08:00:00.000Z", "santa 3.jpg": "2026-11-22T09:00:00.000Z",
                 "vassena 2.jpg": "2026-11-22T09:00:00.000Z", "vassena 3.jpg": "2026-11-21T08:00:00.000Z"},
    )
    state = photos(CONFIG, fetch)
    assert fetched(fetch) == ["santa.jpg", "vassena 2.jpg"]
    assert state.photos == {"santa": prepare(picture((900, 600))), "vassena": prepare(picture((300, 200)))}
    # A name that only happens to start the same way is another word; one the phone gave says nothing
    # of the house. Such pictures are counted, so that the log can say they are there: never named.
    other = bucket({"santarosa.jpg": picture(), "IMG_0001.jpeg": picture(), "foto.HEIC": b"...", "spese.xlsx": b"PK...", "santa.txt": b"..."})
    state = photos(CONFIG, other)
    assert state.absent == ["santa", "vassena"] and other.asked == [] and state.stray == 3


def test_of_two_houses_the_one_named_in_full_has_the_photo():
    twins = replace(CONFIG, properties=(
        PropertyConfig(id="santa", label="Santa", env="SANTA"),
        PropertyConfig(id="santa-alta", label="Santa alta", env="ALTA"),
    ))
    fetch = bucket({"santa-alta.jpg": picture((900, 600)), "Santa-Alta 2.jpg": picture((300, 200)), "santa 2.jpg": picture((600, 400))})
    state = photos(twins, fetch)
    assert fetched(fetch) == ["santa 2.jpg", "santa-alta.jpg"]
    assert state.photos == {"santa": prepare(picture((600, 400))), "santa-alta": prepare(picture((900, 600)))}


def test_a_name_left_empty_by_a_failed_upload_is_passed_over_for_the_next_one():
    # The phone left "IMG_0001.jpeg" in the bucket with nothing in it; the photo was then
    # uploaded again under the house's own name. The empty one need not be removed first.
    named = replace(CONFIG, properties=(replace(CONFIG.properties[0], photo="IMG_0001.jpeg"), *CONFIG.properties[1:]))
    fetch = bucket({"IMG_0001.jpeg": b"", "santa.jpg": b"", "santa.jpeg": picture((900, 600)), "vassena.jpg": picture()})
    state = photos(named, fetch)
    assert fetched(fetch) == ["santa.jpeg", "vassena.jpg"]  # the listing says which are empty: they are not even asked for
    assert state.photos == {"santa": prepare(picture((900, 600))), "vassena": prepare(picture())}
    assert state.fresh == ["santa", "vassena"] and state.warnings == [] and state.absent == []

    # With no other file holding the photo, the empty ones are what there is, and are said to be so.
    fetch = bucket({"IMG_0001.jpeg": b"", "vassena.jpg": b"", "Vassena 2.jpg": b""})
    state = photos(named, fetch, previous=published_with_photos)
    assert state.photos == ONLINE and state.fresh == [] and state.absent == [] and fetch.asked == []
    assert state.warnings == [
        "foto non lette (il file è vuoto (0 byte): il caricamento nel bucket non è riuscito, va caricato di nuovo): "
        "santa; restano quelle già pubblicate",
        "foto non lette (i 2 file con il nome della casa sono tutti vuoti (0 byte): "
        "il caricamento nel bucket non è riuscito, vanno caricati di nuovo): vassena; restano quelle già pubblicate",
    ]
    # A listing that does not say how big a file is leaves it to the file itself.
    unsized = bucket({"santa.jpg": b""})
    unsized.listing = lambda url, key_id, secret: [r2.Entry("santa.jpg")]
    state = photos(CONFIG, unsized, previous=published_with_photos)
    assert fetched(unsized) == ["santa.jpg"] and "il file è vuoto (0 byte)" in state.warnings[0]


def test_a_file_that_is_no_photo_is_passed_over_for_the_next_one_too():
    # An iPhone's photo uploaded as it is, then again as a JPEG: the second is the photo.
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
    fetch = bucket({"santa.jpg": heic, "santa 2.jpg": picture((900, 600)), "vassena.heic": heic})
    state = photos(CONFIG, fetch, previous=published_with_photos)
    assert fetched(fetch) == ["santa.jpg", "santa 2.jpg", "vassena.heic"]
    assert state.photos == {"santa": prepare(picture((900, 600))), "vassena": ONLINE["vassena"]} and state.fresh == ["santa"]
    # A photo kept in a format that is not read is still the house's photo: said to be of the wrong kind, not missing.
    assert state.absent == [] and state.warnings == [
        "foto non lette (il file è in formato HEIC, quello delle foto di iPhone: va caricato come JPEG (88 byte)): "
        "vassena; restano quelle già pubblicate"
    ]
    # Of several that cannot be read, the fault told is that of the likeliest file.
    fetch = bucket({"santa.jpg": b"an .heic renamed", "santa 2.jpg": heic})
    state = photos(CONFIG, fetch, previous=published_with_photos)
    assert state.warnings == [
        "foto non lette (il file non è un'immagine leggibile (JPEG, PNG o WebP; è di 16 byte)): santa; restano quelle già pubblicate"
    ]


def test_a_photo_that_cannot_be_fetched_or_read_keeps_the_one_already_published():
    refused = r2.StorageError("chiave rifiutata, oppure senza il permesso di lettura su questo bucket")
    state = photos(CONFIG, bucket({"santa.jpg": refused, "vassena.jpg": refused}), previous=published_with_photos)
    assert state.photos == ONLINE and state.fresh == []
    assert state.warnings == [
        "foto non lette (chiave rifiutata, oppure senza il permesso di lettura su questo bucket): "
        "santa, vassena; restano quelle già pubblicate"
    ]  # one cause, said once

    # One file is not a photo: the other house is read as usual.
    state = photos(CONFIG, bucket({"santa.jpg": b"an .heic renamed", "vassena.jpg": picture()}), previous=published_with_photos)
    assert state.photos == {"santa": ONLINE["santa"], "vassena": prepare(picture())} and state.fresh == ["vassena"]
    assert state.warnings == [
        "foto non lette (il file non è un'immagine leggibile (JPEG, PNG o WebP; è di 16 byte)): santa; restano quelle già pubblicate"
    ]

    # Whatever else goes wrong must not stop the calendar, nor reach the public log in full.
    state = photos(CONFIG, bucket({"santa.jpg": RuntimeError(f"boom {BUCKET['secret']}"), "vassena.jpg": picture()}),
                   previous=published_with_photos)
    assert state.photos["santa"] == ONLINE["santa"] and state.fresh == ["vassena"]
    assert state.warnings[0].startswith("foto non lette (RuntimeError (sync.py:") and BUCKET["secret"] not in state.warnings[0]

    # A file removed between the listing and its fetch is as if it had never been there.
    gone = bucket({"santa.jpg": r2.NotFound("file non trovato"), "vassena.jpg": picture()})
    state = photos(CONFIG, gone, previous=published_with_photos)
    assert state.absent == ["santa"] and state.fresh == ["vassena"] and state.warnings == []
    # One too large to be a photo does not keep the next from being read.
    huge = r2.StorageError("file troppo grande (oltre 20 MB)")
    state = photos(CONFIG, bucket({"santa.jpg": huge, "santa 2.jpg": picture((900, 600))}), previous=published_with_photos)
    assert state.photos["santa"] == prepare(picture((900, 600))) and state.warnings == []

    # With nothing published yet there is no picture to keep.
    state = photos(CONFIG, bucket({"santa.jpg": refused}), previous=PreviousPage)
    assert state.photos == {} and len(state.warnings) == 1


def test_a_bucket_that_cannot_be_listed_keeps_every_photo_already_published():
    def refused(url, key_id, secret):
        raise r2.StorageError("accesso negato: la chiave non può leggere questo bucket")

    state = photos_state(CONFIG, **BUCKET, load_previous=published_with_photos, fetch=never_fetched, listing=refused)
    assert state.photos == ONLINE and state.fresh == [] and state.absent == []
    assert state.warnings == [
        "foto non lette (accesso negato: la chiave non può leggere questo bucket): santa, vassena; restano quelle già pubblicate"
    ]

    def broken(url, key_id, secret):
        raise RuntimeError(f"boom {BUCKET['secret']}")

    state = photos_state(CONFIG, **BUCKET, load_previous=published_with_photos, fetch=never_fetched, listing=broken)
    assert state.photos == ONLINE and state.warnings[0].startswith("foto non lette (RuntimeError (sync.py:")
    assert BUCKET["secret"] not in state.warnings[0]


def test_a_house_may_name_its_photo_and_say_where_its_subject_is():
    # The owner uploaded the photos as the phone called them, one of them upright with the house near its top.
    named = Config(
        title="Calendario case",
        site_id="test/site",
        properties=(
            PropertyConfig(id="santa", label="Santa", env="SANTA", photo="IMG_0001.jpeg"),
            PropertyConfig(id="vassena", label="Vassena", env="VASSENA", photo="da whatsapp.jpeg", photo_focus=20),
        ),
    )
    upright = upright_picture()
    fetch = bucket({"IMG_0001.jpeg": picture(), "da whatsapp.jpeg": upright, "santa.jpg": picture((300, 200))})
    state = photos(named, fetch, url=FOLDER)  # the bucket's own address
    assert [url.rsplit("/", 1)[1] for url, *_ in fetch.asked] == ["IMG_0001.jpeg", "da%20whatsapp.jpeg"]
    assert state.photos == {"santa": prepare(picture()), "vassena": prepare(upright, 20)}
    assert prepare(upright, 20) != prepare(upright)  # cut around the point the house says
    assert state.fresh == ["santa", "vassena"] and state.warnings == [] and state.stray == 0
    # A named file that is no longer there falls back to the house's own name.
    fetch = bucket({"santa.jpg": picture((300, 200))})
    state = photos(named, fetch)
    assert fetched(fetch) == ["santa.jpg"]
    assert state.photos == {"santa": prepare(picture((300, 200)))} and state.absent == ["vassena"]


@pytest.mark.parametrize("missing", ["url", "key_id", "secret"])
def test_an_incomplete_setting_leaves_the_photos_as_they_are(missing):
    # The costs, read with the same settings, have already said what is missing.
    settings = {**BUCKET, missing: None}
    state = photos_state(CONFIG, **settings, load_previous=published_with_photos, fetch=never_fetched, listing=never_fetched)
    assert state == PhotosState(ONLINE, [], [])


def test_the_photos_are_listed_and_read_through_the_signed_requests_unless_told_otherwise(monkeypatch):
    fetch = bucket({"santa.jpg": picture()})
    monkeypatch.setattr(r2, "fetch", fetch)
    monkeypatch.setattr(r2, "entries", fetch.listing)
    state = photos_state(CONFIG, **BUCKET, load_previous=no_previous)
    assert state.fresh == ["santa"] and state.absent == ["vassena"]
    assert len(fetch.listed) == 1 and fetched(fetch) == ["santa.jpg"]


def test_the_photos_travel_with_the_calendar_and_are_recovered_from_the_published_page(tmp_path, monkeypatch):
    photos = {"santa": prepare(picture())}
    snapshot = Snapshot(generated_at=NOW, properties=[house("santa"), house("vassena")], photos=photos)
    envelope = _published_envelope(tmp_path, snapshot)
    family, figures = open_envelope(envelope, passphrase=FAMILY)
    assert family["photos"] == {"santa": photos["santa"].to_dict()}
    assert "photos" not in figures
    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: envelope)
    assert previous_from_site("https://x.example", passphrase=FAMILY).photos == photos
    # A page published before the photos existed simply has none.
    older = _published_envelope(tmp_path / "older", Snapshot(generated_at=NOW, properties=[house("santa")]))
    older_family, _ = open_envelope(older, passphrase=FAMILY)
    assert older_family["photos"] == {}
    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: older)
    assert previous_from_site("https://x.example", passphrase=FAMILY).photos == {}
