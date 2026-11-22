import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest

from casecal import cli, sync
from casecal.config import (
    OPTIONAL_SECRETS,
    ConfigError,
    credentials_for,
    family_groups,
    load_config,
    missing_secrets,
)
from casecal.costs import CostLine, CostSheet
from casecal.crypto import derive_key, open_envelope, site_salt
from casecal.demo import DEMO_PASSPHRASE, demo_snapshot
from casecal.errors import TransportError
from casecal.family import encode_event
from casecal.familylog import ISSUE_TITLE, GitHubLog
from casecal.model import BOOKING, CLOSED, PropertySnapshot
from casecal.photos import MAX_SOURCE_BYTES, prepare
from casecal.r2 import NotFound, StorageError
from casecal.site import extract_envelope
from github_fake import TOKEN, FakeGitHub
from pictures import picture
from workbooks import workbook, year_rows

ROOT = Path(__file__).resolve().parent.parent

OLD_OWNER_PASSPHRASE = "lanterna-golfo-ardesia-17"  # opened the figures before the single access
GOOD_ENV = {
    "FAMILY_PASSPHRASE": "nebbia-quercia-sentiero-42",  # the page's one passphrase
    "SANTA_KB_URL": "https://portal.example/a/",
    "SANTA_KB_USER": "user-a",
    "SANTA_KB_PASS": "Portal-pass-a",
    "VASSENA_KB_URL": "https://portal.example/b",
    "VASSENA_KB_USER": "user b",
    "VASSENA_KB_PASS": "Portal-pass-b",
}


def write_config(tmp_path, text):
    path = tmp_path / "properties.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_repository_configuration_is_valid():
    config = load_config(ROOT / "properties.toml")
    assert [p.id for p in config.properties] == ["santa", "vassena"]
    assert [(p.check_in, p.check_out) for p in config.properties] == [("15:00", "11:00")] * 2
    # Only the second house has its owners' stays entered as bookings that bill nothing.
    assert [p.unpaid_is_own_use for p in config.properties] == [False, True]
    assert config.timezone == "Europe/Rome"
    assert config.properties[0].secret_names() == ("SANTA_KB_URL", "SANTA_KB_USER", "SANTA_KB_PASS")


def test_the_repository_configuration_holds_no_secrets():
    text = (ROOT / "properties.toml").read_text(encoding="utf-8").lower()
    for forbidden in ("http", "password =", "pass =", "user ="):
        assert forbidden not in text


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "nessuna"),
        ('[[property]]\nid = "a"\nlabel = "A"\n', "env"),
        ('[[property]]\nid = "Bad Id"\nlabel = "A"\nenv = "A"\n', "id non valido"),
        ('[[property]]\nid = "a"\nlabel = "A"\nenv = "lower"\n', "env non valido"),
        ('[[property]]\nid = "a"\nlabel = "A"\nenv = "A"\n[[property]]\nid = "a"\nlabel = "B"\nenv = "B"\n', "duplicato"),
        ('years_ahead = 9\n[[property]]\nid = "a"\nlabel = "A"\nenv = "A"\n', "years_ahead"),
        ("this is not toml", "TOML"),
    ],
)
def test_invalid_configuration_is_explained(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, text))


HOUSE = '[[property]]\nid = "a"\nlabel = "A"\nenv = "A"\n'


def test_house_hours_default_to_three_and_eleven(tmp_path):
    prop = load_config(write_config(tmp_path, HOUSE)).properties[0]
    assert (prop.check_in, prop.check_out) == ("15:00", "11:00")


def test_house_hours_can_be_set_for_all_houses_and_overridden_for_one(tmp_path):
    text = (
        'check_in = "16:00"\ncheck_out = "10:30"\n' + HOUSE
        + '[[property]]\nid = "b"\nlabel = "B"\nenv = "B"\ncheck_in = "14:00"\n'
    )
    first, second = load_config(write_config(tmp_path, text)).properties
    assert (first.check_in, first.check_out) == ("16:00", "10:30")
    assert (second.check_in, second.check_out) == ("14:00", "10:30")  # only the override changes


@pytest.mark.parametrize(
    "text, message",
    [
        ('check_in = "3pm"\n' + HOUSE, "check_in deve essere un orario HH:MM"),
        ('check_out = "24:00"\n' + HOUSE, "check_out deve essere un orario HH:MM"),
        ('check_in = "9:00"\n' + HOUSE, "check_in deve essere un orario HH:MM"),
        (HOUSE + 'check_out = "11.00"\n', "a: check_out deve essere un orario HH:MM"),
        ('check_in = "10:00"\ncheck_out = "11:00"\n' + HOUSE, "check_out deve precedere check_in"),
        (HOUSE + 'check_in = "11:00"\n', "a: check_out deve precedere check_in"),
    ],
)
def test_impossible_house_hours_are_refused(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, text))


def test_a_house_may_have_a_photo_under_a_name_of_its_own(tmp_path):
    plain = load_config(write_config(tmp_path, '[[property]]\nid = "a"\nlabel = "A"\nenv = "A"\n'))
    assert (plain.properties[0].photo, plain.properties[0].photo_focus) == (None, 50)
    named = load_config(write_config(
        tmp_path, '[[property]]\nid = "a"\nlabel = "A"\nenv = "A"\nphoto = " IMG 0001.jpeg "\nphoto_focus = 26\n'))
    assert (named.properties[0].photo, named.properties[0].photo_focus) == ("IMG 0001.jpeg", 26)


@pytest.mark.parametrize(
    "line, message",
    [
        ('photo = ""', "photo deve essere il nome di un file del bucket"),
        ('photo = "cartella/foto.jpg"', "photo deve essere il nome di un file del bucket"),
        ("photo = 7", "photo deve essere il nome di un file del bucket"),
        ("photo_focus = 101", "photo_focus deve essere un numero intero tra 0"),
        ("photo_focus = -1", "photo_focus deve essere un numero intero tra 0"),
        ("photo_focus = 26.5", "photo_focus deve essere un numero intero tra 0"),
        ('photo_focus = "alto"', "photo_focus deve essere un numero intero tra 0"),
        ("photo_focus = true", "photo_focus deve essere un numero intero tra 0"),
    ],
)
def test_an_impossible_photo_setting_is_refused(tmp_path, line, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, f'[[property]]\nid = "a"\nlabel = "A"\nenv = "A"\n{line}\n'))


def test_missing_file_is_a_configuration_error(tmp_path):
    with pytest.raises(ConfigError, match="non trovato"):
        load_config(tmp_path / "nope.toml")


def test_the_own_use_rule_is_a_yes_or_no_per_house(tmp_path):
    config = load_config(write_config(tmp_path, HOUSE + "unpaid_is_own_use = true\n"))
    assert config.properties[0].unpaid_is_own_use is True
    assert load_config(write_config(tmp_path, HOUSE)).properties[0].unpaid_is_own_use is False
    with pytest.raises(ConfigError, match="a: unpaid_is_own_use deve essere true o false"):
        load_config(write_config(tmp_path, HOUSE + 'unpaid_is_own_use = "sì"\n'))


def test_the_family_groups_are_read_from_their_secret():
    assert family_groups({}) == ([], [])
    assert family_groups({"FAMILY_GROUPS": "  "}) == ([], [])
    assert family_groups({"FAMILY_GROUPS": "Famiglia Rossi; Famiglia  Bianchi ;"}) == (["Famiglia Rossi", "Famiglia Bianchi"], [])
    assert family_groups({"FAMILY_GROUPS": "Famiglia Rossi\nFamiglia Bianchi\n"})[0] == ["Famiglia Rossi", "Famiglia Bianchi"]


def test_a_slip_in_the_family_groups_is_trimmed_and_reported_without_names():
    names, problems = family_groups({"FAMILY_GROUPS": "Rossi; rossi; " + "x" * 41 + "; Bian\tchi"})
    assert names == ["Rossi", "Bian chi"]
    assert len(problems) == 2 and all("FAMILY_GROUPS" in problem for problem in problems)
    assert not any("Rossi" in problem or "xxx" in problem for problem in problems)
    many, problems = family_groups({"FAMILY_GROUPS": ";".join(f"Famiglia {n}" for n in range(12))})
    assert len(many) == 8 and len(problems) == 1 and "più di 8" in problems[0]


def test_the_nightly_workflow_passes_every_secret_the_program_needs():
    workflow = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    wired = set(re.findall(r"^\s+([A-Z0-9_]+): \$\{\{ secrets\.\1 \}\}$", workflow, re.MULTILINE))
    needed = set(missing_secrets(load_config(ROOT / "properties.toml"), {}))
    # Adding a house to properties.toml means adding its secrets to the workflow.
    assert wired == needed | set(OPTIONAL_SECRETS)
    assert not needed & set(OPTIONAL_SECRETS)  # the program must start without the optional ones


def test_missing_secrets_are_listed_by_name():
    config = load_config(ROOT / "properties.toml")
    assert missing_secrets(config, GOOD_ENV) == []
    assert "OWNER_PASSPHRASE" not in GOOD_ENV  # one passphrase is all the page needs
    partial = {k: v for k, v in GOOD_ENV.items() if k not in ("FAMILY_PASSPHRASE", "VASSENA_KB_PASS")}
    partial["SANTA_KB_USER"] = "   "
    assert missing_secrets(config, partial) == ["FAMILY_PASSPHRASE", "SANTA_KB_USER", "VASSENA_KB_PASS"]


def test_credentials_are_read_from_the_environment():
    config = load_config(ROOT / "properties.toml")
    santa = credentials_for(config.properties[0], {**GOOD_ENV, "SANTA_KB_UNIT": " Bilocale Demo "})
    assert santa.base_url == "https://portal.example/a"  # trailing slash dropped
    assert (santa.username, santa.password, santa.unit) == ("user-a", "Portal-pass-a", "Bilocale Demo")
    vassena = credentials_for(config.properties[1], GOOD_ENV)
    assert vassena.username == "user b" and vassena.unit is None
    with pytest.raises(ConfigError, match="https"):
        credentials_for(config.properties[0], {**GOOD_ENV, "SANTA_KB_URL": "http://portal.example/a"})


def run_cli(monkeypatch, capsys, argv, env=None):
    ambient = ["GITHUB_ACTIONS", "GITHUB_OUTPUT", "GITHUB_REPOSITORY", "GITHUB_REPOSITORY_OWNER",
               "GITHUB_API_URL", "SITE_URL", "ALERTS_TOKEN"]
    for name in list(GOOD_ENV) + list(OPTIONAL_SECRETS) + ambient:
        monkeypatch.delenv(name, raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(ROOT)
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_check_passes_with_a_complete_environment(monkeypatch, capsys):
    code, output = run_cli(monkeypatch, capsys, ["check"], GOOD_ENV)
    assert code == cli.EXIT_OK
    assert "2 case" in output
    for value in GOOD_ENV.values():
        assert value not in output  # a public log must never show a secret


def test_check_says_whether_the_family_can_edit(monkeypatch, capsys):
    _, output = run_cli(monkeypatch, capsys, ["check"], GOOD_ENV)
    assert "registro di famiglia: non attivo" in output
    _, output = run_cli(monkeypatch, capsys, ["check"], {**GOOD_ENV, "FAMILY_LOG_TOKEN": TOKEN})
    assert "registro di famiglia: attivo" in output and TOKEN not in output
    code, output = run_cli(monkeypatch, capsys, ["check"], {**GOOD_ENV, "FAMILY_LOG_TOKEN": "ghp_classic0123456789"})
    assert code == cli.EXIT_OK  # the calendar itself is fine
    assert "fine-grained" in output and "ghp_classic0123456789" not in output


# Where the owner's workbook of costs is kept, and the read-only key that fetches it.
BUCKET_ENV = {
    "COSTS_R2_URL": "https://acc0123.r2.cloudflarestorage.com/case-private/spese.xlsx",
    "COSTS_R2_ACCESS_KEY_ID": "key-id-0123456789",
    "COSTS_R2_SECRET_ACCESS_KEY": "key-secret-9876543210",
}


def test_check_says_whether_the_costs_behind_the_profit_and_loss_are_set_up(monkeypatch, capsys):
    _, output = run_cli(monkeypatch, capsys, ["check"], GOOD_ENV)
    assert "spese per il P&L: non configurate" in output
    code, output = run_cli(monkeypatch, capsys, ["check"], {**GOOD_ENV, **BUCKET_ENV})
    assert code == cli.EXIT_OK and "spese per il P&L: configurate" in output
    for value in BUCKET_ENV.values():
        assert value not in output
    half = {**GOOD_ENV, "COSTS_R2_URL": BUCKET_ENV["COSTS_R2_URL"]}
    code, output = run_cli(monkeypatch, capsys, ["check"], half)
    assert code == cli.EXIT_OK  # the calendar itself is fine
    assert "configurazione incompleta, manca COSTS_R2_ACCESS_KEY_ID, COSTS_R2_SECRET_ACCESS_KEY" in output
    assert BUCKET_ENV["COSTS_R2_URL"] not in output


def test_check_names_what_is_missing_without_printing_values(monkeypatch, capsys):
    env = {k: v for k, v in GOOD_ENV.items() if k != "VASSENA_KB_PASS"}
    code, output = run_cli(monkeypatch, capsys, ["check"], env)
    assert code == cli.EXIT_CONFIG
    assert "VASSENA_KB_PASS" in output
    for value in env.values():
        assert value not in output


def test_check_rejects_a_passphrase_reused_from_a_portal(monkeypatch, capsys):
    env = {**GOOD_ENV, "FAMILY_PASSPHRASE": "casa-Portal-pass-a-2026"}
    code, output = run_cli(monkeypatch, capsys, ["check"], env)
    assert code == cli.EXIT_CONFIG
    assert "credenziali" in output and "Portal-pass-a" not in output


def test_problems_become_annotations_inside_github_actions(monkeypatch, capsys):
    code, output = run_cli(monkeypatch, capsys, ["check"], {"GITHUB_ACTIONS": "true"})
    assert code == cli.EXIT_CONFIG
    assert output.startswith("::error::secret mancanti: FAMILY_PASSPHRASE")


def test_passphrase_command_prints_a_usable_passphrase(monkeypatch, capsys):
    code, output = run_cli(monkeypatch, capsys, ["passphrase"])
    assert code == cli.EXIT_OK
    assert len(output.strip()) >= 20 and output.strip().count("-") == 4


# --------------------------------------------------------------------------- the nightly command

ENTRY = {"id": "e_k3j9x2m1q8", "prop": "santa", "start": "2027-07-03", "end": "2027-07-10",
         "who": "Ermenegilda", "note": "", "by": "Ermenegilda", "at": "2026-11-20T12:30:00Z"}


def empty_house(prop, credentials, years, now):
    return PropertySnapshot(id=prop.id, label=prop.label, fetched_at=now,
                            covered_from=date(years[0], 1, 1), covered_to=date(years[-1] + 1, 1, 1))


def run_sync(monkeypatch, capsys, tmp_path, github, **env):
    """Run ``casecal sync`` against invented portals and a stand-in for GitHub."""
    monkeypatch.setattr("casecal.portal.fetch_property", empty_house)
    if github is None:
        def nothing_to_talk_to(*args, **kwargs):
            raise AssertionError("GitHub must not be contacted in this scenario")
        monkeypatch.setattr(sync, "GitHubLog", nothing_to_talk_to)
    else:
        monkeypatch.setattr(sync, "GitHubLog", lambda token, repo, api: GitHubLog(token, repo, api=api, session=github))
    outputs = tmp_path / "outputs.txt"
    settings = {**GOOD_ENV, "GITHUB_REPOSITORY": "owner/repo", "GITHUB_OUTPUT": str(outputs), **env}
    code, printed = run_cli(monkeypatch, capsys, ["sync", "--out", str(tmp_path / "site")], settings)
    assert code == cli.EXIT_OK
    for secret in list(GOOD_ENV.values()) + [value for name, value in env.items() if name in OPTIONAL_SECRETS]:
        assert secret not in printed  # a public log must never show a secret
    page = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    family, figures = open_envelope(extract_envelope(page), passphrase=GOOD_ENV["FAMILY_PASSPHRASE"])
    assert figures is not None  # the one passphrase opens the figures too
    return family, printed, outputs.read_text(encoding="utf-8")


def test_sync_publishes_the_family_entries_and_the_way_to_edit_them(monkeypatch, capsys, tmp_path):
    key = derive_key(GOOD_ENV["FAMILY_PASSPHRASE"], site_salt("owner/repo"))
    github = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE}],
                        comments={7: [encode_event(key, {"t": "set", **ENTRY})]})
    family, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, github, FAMILY_LOG_TOKEN=TOKEN)
    assert family["entries"] == [ENTRY]
    assert family["log"] == {"kind": "github", "api": "https://api.github.com", "repo": "owner/repo", "issue": 7,
                             "token": TOKEN, "cursor": 1000, "since": "2026-11-20T11:00:00Z"}
    assert "registro di famiglia: letto" in printed and "degraded=false" in outputs
    assert all(call[0] == "GET" for call in github.calls), "a log that exists is only read"


def test_sync_without_a_token_publishes_a_read_only_page(monkeypatch, capsys, tmp_path):
    family, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, None)
    assert family["entries"] == [] and family["log"] is None
    assert "registro" not in printed and "degraded=false" in outputs


def test_sync_with_a_refused_token_still_publishes_and_asks_for_attention(monkeypatch, capsys, tmp_path):
    family, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, FakeGitHub(status=401), FAMILY_LOG_TOKEN=TOKEN)
    assert len(family["properties"]) == 2  # the calendar is published all the same
    assert family["log"] is None  # nothing was online before: no settings to keep
    assert "registro: registro di famiglia non letto" in printed and "token rifiutato" in printed
    assert "degraded=true" in outputs  # the job fails after publishing, so that GitHub writes to the owner


def test_sync_never_publishes_a_token_of_the_wrong_kind(monkeypatch, capsys, tmp_path):
    family, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, None, FAMILY_LOG_TOKEN="ghp_classic0123456789")
    assert family["log"] is None
    assert "fine-grained" in printed and "degraded=true" in outputs


def test_sync_publishes_the_families_the_form_offers(monkeypatch, capsys, tmp_path):
    family, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, None, FAMILY_GROUPS="Famiglia Rossi; Famiglia Bianchi")
    assert family["groups"] == ["Famiglia Rossi", "Famiglia Bianchi"]
    assert "degraded=false" in outputs and "Rossi" not in printed
    # A slip in that secret does not stop the calendar: it is trimmed, and the run asks for attention.
    family, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, None, FAMILY_GROUPS="Famiglia Rossi; famiglia rossi")
    assert family["groups"] == ["Famiglia Rossi"]
    assert "FAMILY_GROUPS: un nome è ripetuto" in printed and "degraded=true" in outputs


def test_sync_hands_the_owners_alert_what_it_needs(monkeypatch, capsys, tmp_path):
    seen = {}

    def fake_alert(config, snapshot, report, family, **settings):
        seen.update(settings, fresh=family.fresh, entries=len(family.entries))
        return "open", None

    monkeypatch.setattr(cli, "alert_owner", fake_alert)
    key = derive_key(GOOD_ENV["FAMILY_PASSPHRASE"], site_salt("owner/repo"))
    github = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE}],
                        comments={7: [encode_event(key, {"t": "set", **ENTRY})]})
    _, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, github, FAMILY_LOG_TOKEN=TOKEN,
                                   ALERTS_TOKEN="ghs_token-del-workflow", GITHUB_REPOSITORY_OWNER="owner")
    assert (seen["token"], seen["repository"], seen["owner"]) == ("ghs_token-del-workflow", "owner/repo", "owner")
    assert seen["fresh"] is True and seen["entries"] == 1
    assert "avviso al proprietario: segnalazione aperta" in printed and "ghs_token-del-workflow" not in printed
    assert "degraded=false" in outputs


def test_sync_reports_an_alert_that_could_not_be_written(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "alert_owner", lambda *args, **settings: (None, "avviso al proprietario non aggiornato (HTTP 500)"))
    family, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, None)
    assert len(family["properties"]) == 2  # the page is published all the same
    assert "avvisi: avviso al proprietario non aggiornato" in printed and "degraded=true" in outputs


def published_figures(tmp_path):
    page = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    return open_envelope(extract_envelope(page), passphrase=GOOD_ENV["FAMILY_PASSPHRASE"])[1]


def stand_in_bucket(files):
    """What the bucket answers: ``files`` maps a file name to its bytes; any other name is not there."""
    asked = []

    def fetch(url, key_id, secret, **limits):
        asked.append((url, key_id, secret, limits))
        name = url.rsplit("/", 1)[1]
        if name not in files:
            raise NotFound("file non trovato: controlla il nome del bucket e del file in COSTS_R2_URL")
        return files[name]

    fetch.asked = asked
    return fetch


def test_sync_publishes_the_costs_it_reads_from_the_bucket(monkeypatch, capsys, tmp_path):
    bucket = stand_in_bucket({"spese.xlsx": workbook({"2026": year_rows("Vassena", "Santa Margherita")})})
    monkeypatch.setattr("casecal.r2.fetch", bucket)
    family, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, None, **BUCKET_ENV)
    assert bucket.asked[0] == (*BUCKET_ENV.values(), {})
    assert "spese: lette" in printed and "degraded=false" in outputs
    # No photo is kept beside the workbook: the houses have none. That is no fault, and the log
    # says so in a line of its own, where a photo uploaded under another name would be looked for.
    assert family["photos"] == {}
    assert "foto: nessun file per santa, vassena (atteso <casa>.jpg accanto al foglio delle spese)" in printed
    costs = published_figures(tmp_path)["costs"]
    assert [sheet["year"] for sheet in costs] == [2026]
    assert costs[0]["lines"][0] == {"label": "TARI", "amounts": {"vassena": 54000, "santa": 18000}, "note": "saldo a dicembre"}
    for private in ("TARI", "saldo a dicembre", "540"):
        assert private not in printed  # the workbook is private, the log is not


def test_sync_without_the_settings_of_the_costs_publishes_no_profit_and_loss(monkeypatch, capsys, tmp_path):
    def never(*settings, **limits):
        raise AssertionError("the bucket must not be contacted in this scenario")

    monkeypatch.setattr("casecal.r2.fetch", never)
    family, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, None)
    assert published_figures(tmp_path)["costs"] == [] and family["photos"] == {}
    assert "spese" not in printed and "foto" not in printed and "degraded=false" in outputs


def test_sync_publishes_the_photos_kept_beside_the_workbook(monkeypatch, capsys, tmp_path):
    source = picture((1600, 1200))
    bucket = stand_in_bucket({"spese.xlsx": workbook({"2026": year_rows("Vassena", "Santa")}), "santa.jpg": source})
    monkeypatch.setattr("casecal.r2.fetch", bucket)
    family, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, None, **BUCKET_ENV)
    folder = BUCKET_ENV["COSTS_R2_URL"].rsplit("/", 1)[0]
    # Each house's photo is looked for under the name the configuration gives it, if any,
    # then under the house's own name with a photo's usual endings; the first found is it.
    houses = {prop.id: prop for prop in load_config(ROOT / "properties.toml").properties}
    named = {house: ([quote(prop.photo)] if prop.photo else []) for house, prop in houses.items()}
    assert [(url, limits) for url, _, _, limits in bucket.asked[1:]] == [
        (f"{folder}/{name}", {"max_bytes": MAX_SOURCE_BYTES})
        for name in named["santa"] + ["santa.jpg"] + named["vassena"] + ["vassena.jpg", "vassena.jpeg", "vassena.png", "vassena.webp"]
    ]
    # The house with a file has its picture, made small; the other one has none.
    assert family["photos"] == {"santa": prepare(source, houses["santa"].photo_focus).to_dict()}
    assert "foto: lette (santa)" in printed and "foto: nessun file per vassena" in printed
    assert "degraded=false" in outputs
    assert "photos" not in published_figures(tmp_path)


def test_sync_finds_the_workbook_in_a_bucket_given_by_its_address_alone(monkeypatch, capsys, tmp_path):
    address = "https://acc0123.r2.cloudflarestorage.com/case-private"  # as Cloudflare shows it for the bucket
    bucket = stand_in_bucket({"Analisi%20Entrate-Spese.xlsx": workbook({"2026": year_rows("Vassena", "Santa")})})
    listed = []
    monkeypatch.setattr("casecal.r2.fetch", bucket)
    monkeypatch.setattr("casecal.r2.names", lambda *settings: listed.append(settings) or ["IMG_0001.jpeg", "Analisi Entrate-Spese.xlsx"])
    _, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, None, **{**BUCKET_ENV, "COSTS_R2_URL": address})
    assert listed == [(address, BUCKET_ENV["COSTS_R2_ACCESS_KEY_ID"], BUCKET_ENV["COSTS_R2_SECRET_ACCESS_KEY"])]
    assert bucket.asked[0][0] == f"{address}/Analisi%20Entrate-Spese.xlsx"
    assert "spese: lette" in printed and "degraded=false" in outputs
    assert [sheet["year"] for sheet in published_figures(tmp_path)["costs"]] == [2026]
    assert "Analisi" not in printed  # the name of the file is the owner's business


def test_sync_keeps_the_photos_already_published_when_one_cannot_be_read(monkeypatch, capsys, tmp_path):
    online = prepare(picture((900, 600)))
    bucket = stand_in_bucket({"spese.xlsx": workbook({"2026": year_rows("Vassena", "Santa")}), "santa.jpg": b"not a photo"})
    monkeypatch.setattr("casecal.r2.fetch", bucket)
    monkeypatch.setattr(cli, "previous_from_site", lambda site_url, **settings: sync.PreviousPage(photos={"santa": online}))
    family, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, None, **BUCKET_ENV)
    assert family["photos"] == {"santa": online.to_dict()}
    assert "foto: foto non lette (il file non è un'immagine leggibile (JPEG, PNG o WebP)): santa" in printed
    assert "degraded=true" in outputs  # the page is published, then the run asks for attention


def test_sync_keeps_the_costs_already_published_when_the_bucket_cannot_be_read(monkeypatch, capsys, tmp_path):
    online = CostSheet(year=2026, lines=(CostLine("IMU", {"santa": 145000, "vassena": 390050}, "stima"),))

    def refused(url, key_id, secret, **limits):
        raise StorageError("chiave rifiutata, oppure senza il permesso di lettura su questo bucket")

    monkeypatch.setattr("casecal.r2.fetch", refused)
    monkeypatch.setattr(cli, "previous_from_site", lambda site_url, **settings: sync.PreviousPage(costs=[online]))
    _, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, None, **BUCKET_ENV)
    assert published_figures(tmp_path)["costs"] == [online.to_dict()]
    assert "spese: spese non lette (chiave rifiutata" in printed and "restano quelle già pubblicate" in printed
    assert "degraded=true" in outputs  # the page is published, then the run asks for attention


def test_sync_carries_the_entries_over_a_change_of_passphrase(monkeypatch, capsys, tmp_path):
    old = "vecchia-password-di-casa"
    github = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE}],
                        comments={7: [encode_event(derive_key(old, site_salt("owner/repo")), {"t": "set", **ENTRY})]})
    family, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, github,
                                        FAMILY_LOG_TOKEN=TOKEN, OLD_FAMILY_PASSPHRASE=old)
    assert family["entries"] == [ENTRY] and "degraded=false" in outputs
    assert len(github.comments[7]) == 2 and family["log"]["cursor"] == 1001  # rewritten once under the new key


def test_sync_opens_the_page_online_with_the_old_second_passphrase_only_if_still_set(monkeypatch, capsys, tmp_path):
    # A page published when the figures had their own passphrase: the job needs that
    # passphrase to carry the figures of a house over, the one night it finds such a page.
    def vassena_down(prop, credentials, years, now):
        if prop.id == "vassena":
            raise TransportError("nessuna risposta")
        return empty_house(prop, credentials, years, now)

    monkeypatch.setattr("casecal.portal.fetch_property", vassena_down)
    asked = []

    def page_online(site_url, **secrets):
        asked.append((site_url, secrets))
        return cli.PreviousPage()

    monkeypatch.setattr(cli, "previous_from_site", page_online)
    env = {**GOOD_ENV, "SITE_URL": "https://example.github.io/repo"}
    code, printed = run_cli(monkeypatch, capsys, ["sync", "--out", str(tmp_path / "a")],
                            {**env, "OWNER_PASSPHRASE": OLD_OWNER_PASSPHRASE})
    assert code == cli.EXIT_OK and OLD_OWNER_PASSPHRASE not in printed
    assert asked == [("https://example.github.io/repo",
                      {"passphrase": GOOD_ENV["FAMILY_PASSPHRASE"], "legacy_owner_passphrase": OLD_OWNER_PASSPHRASE})]
    asked.clear()
    code, _ = run_cli(monkeypatch, capsys, ["sync", "--out", str(tmp_path / "b")], env)
    assert code == cli.EXIT_OK and asked[0][1]["legacy_owner_passphrase"] is None


@pytest.mark.parametrize("variant", ["ok", "lagging", "missing"])
def test_demo_builds_an_openable_page(monkeypatch, capsys, tmp_path, variant):
    code, output = run_cli(monkeypatch, capsys, ["demo", "--out", str(tmp_path), "--variant", variant])
    assert code == cli.EXIT_OK and "dati inventati" in output
    assert output.count("password") == 1 and DEMO_PASSPHRASE in output  # one password to hand out
    envelope = extract_envelope((tmp_path / "index.html").read_text(encoding="utf-8"))
    family, owner = open_envelope(envelope, passphrase=DEMO_PASSPHRASE)
    assert family["demo"] is True
    assert bool(family["properties"][1].get("missing")) is (variant == "missing")
    assert len(owner["properties"]) == 2
    assert family["log"] == {"kind": "local"}  # edits made in the demo stay in the browser
    assert family["groups"] == ["Famiglia Rossi", "Famiglia Bianchi"]
    # The costs behind the profit and loss: this year's and next year's, for both houses.
    year = datetime.now(timezone.utc).year
    assert [sheet["year"] for sheet in owner["costs"]] in ([year, year + 1], [year + 1, year + 2])  # around midnight
    for sheet in owner["costs"]:
        assert len(sheet["lines"]) >= 10 and {"mare", "lago"} == set(sheet["lines"][0]["amounts"])
        assert any(None in line["amounts"].values() for line in sheet["lines"])  # a cost of one house only
    # A picture of each house, drawn for the demo; also of a house whose portal could not be read.
    assert set(family["photos"]) == {"mare", "lago"}
    assert all(photo["type"] == "image/webp" and photo["data"] for photo in family["photos"].values())
    assert family["photos"]["mare"] != family["photos"]["lago"]
    # Also for a house that could not be read: the family's notes do not depend on the portal.
    assert sorted(entry["prop"] for entry in family["entries"]) == ["lago", "mare", "mare"]


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc),
        datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc),
        datetime(2028, 2, 29, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 15, 2, 17, tzinfo=timezone.utc),
    ],
)
def test_demo_data_is_valid_on_awkward_dates(moment):
    snapshot = demo_snapshot(moment)
    for prop in snapshot.properties:
        assert prop.stays, "each demo house has stays"
        assert all(prop.covered_from <= s.start and s.end <= prop.covered_to for s in prop.stays)
        booked = sum(s.nights for s in prop.stays if s.kind == "booking")
        assert sum(k.room_nights for k in prop.kpis) == booked  # economics agree with the calendar

    # The family's entries show both standings: one on nights the portal has blocked,
    # one on nights still for sale. Neither is in the past or over guests.
    houses = {prop.id: prop for prop in snapshot.properties}
    kinds = {}
    for entry in snapshot.entries:
        assert entry.start > moment.date()
        nights = [entry.start + timedelta(days=n) for n in range((entry.end - entry.start).days)]
        on_each = [next((s.kind for s in houses[entry.prop].stays if s.start <= night < s.end), None) for night in nights]
        assert BOOKING not in on_each
        kinds[entry.id] = set(on_each)
    assert kinds == {"demo_blocked_1": {CLOSED}, "demo_shared_1": {CLOSED}, "demo_pending_1": {None}}
    # Two of them share nights in the same house: the calendar has one bar to draw for both.
    by_id = {entry.id: entry for entry in snapshot.entries}
    first, second = by_id["demo_blocked_1"], by_id["demo_shared_1"]
    assert first.prop == second.prop and first.start < second.start < first.end < second.end
