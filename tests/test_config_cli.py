import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from casecal import cli, sync
from casecal.config import OPTIONAL_SECRETS, ConfigError, credentials_for, load_config, missing_secrets
from casecal.crypto import derive_key, open_envelope, site_salt
from casecal.demo import DEMO_FAMILY_PASSPHRASE, DEMO_OWNER_PASSPHRASE, demo_snapshot
from casecal.family import encode_event
from casecal.familylog import ISSUE_TITLE, GitHubLog
from casecal.model import BOOKING, CLOSED, PropertySnapshot
from casecal.site import extract_envelope
from github_fake import TOKEN, FakeGitHub

ROOT = Path(__file__).resolve().parent.parent

GOOD_ENV = {
    "FAMILY_PASSPHRASE": "nebbia-quercia-sentiero-42",
    "OWNER_PASSPHRASE": "lanterna-golfo-ardesia-17",
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


def test_missing_file_is_a_configuration_error(tmp_path):
    with pytest.raises(ConfigError, match="non trovato"):
        load_config(tmp_path / "nope.toml")


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
    partial = {k: v for k, v in GOOD_ENV.items() if k not in ("OWNER_PASSPHRASE", "VASSENA_KB_PASS")}
    partial["SANTA_KB_USER"] = "   "
    assert missing_secrets(config, partial) == ["OWNER_PASSPHRASE", "SANTA_KB_USER", "VASSENA_KB_PASS"]


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
    ambient = ["GITHUB_ACTIONS", "GITHUB_OUTPUT", "GITHUB_REPOSITORY", "GITHUB_API_URL", "SITE_URL"]
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
    family, _ = open_envelope(extract_envelope(page), family_passphrase=GOOD_ENV["FAMILY_PASSPHRASE"])
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


def test_sync_carries_the_entries_over_a_change_of_passphrase(monkeypatch, capsys, tmp_path):
    old = "vecchia-password-di-casa"
    github = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE}],
                        comments={7: [encode_event(derive_key(old, site_salt("owner/repo")), {"t": "set", **ENTRY})]})
    family, printed, outputs = run_sync(monkeypatch, capsys, tmp_path, github,
                                        FAMILY_LOG_TOKEN=TOKEN, OLD_FAMILY_PASSPHRASE=old)
    assert family["entries"] == [ENTRY] and "degraded=false" in outputs
    assert len(github.comments[7]) == 2 and family["log"]["cursor"] == 1001  # rewritten once under the new key


@pytest.mark.parametrize("variant", ["ok", "lagging", "missing"])
def test_demo_builds_an_openable_page(monkeypatch, capsys, tmp_path, variant):
    code, output = run_cli(monkeypatch, capsys, ["demo", "--out", str(tmp_path), "--variant", variant])
    assert code == cli.EXIT_OK and "dati inventati" in output
    envelope = extract_envelope((tmp_path / "index.html").read_text(encoding="utf-8"))
    family, owner = open_envelope(
        envelope, family_passphrase=DEMO_FAMILY_PASSPHRASE, owner_passphrase=DEMO_OWNER_PASSPHRASE
    )
    assert family["demo"] is True
    assert bool(family["properties"][1].get("missing")) is (variant == "missing")
    assert len(owner["properties"]) == 2
    assert family["log"] == {"kind": "local"}  # edits made in the demo stay in the browser
    # Also for a house that could not be read: the family's notes do not depend on the portal.
    assert sorted(entry["prop"] for entry in family["entries"]) == ["lago", "mare"]


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
    assert kinds == {"demo_blocked_1": {CLOSED}, "demo_pending_1": {None}}
