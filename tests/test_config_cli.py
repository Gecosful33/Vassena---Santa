import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from casecal import cli
from casecal.config import ConfigError, credentials_for, load_config, missing_secrets
from casecal.crypto import open_envelope
from casecal.demo import DEMO_FAMILY_PASSPHRASE, DEMO_OWNER_PASSPHRASE, demo_snapshot
from casecal.site import extract_envelope

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


def test_missing_file_is_a_configuration_error(tmp_path):
    with pytest.raises(ConfigError, match="non trovato"):
        load_config(tmp_path / "nope.toml")


def test_the_nightly_workflow_passes_every_secret_the_program_needs():
    workflow = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    wired = set(re.findall(r"^\s+([A-Z0-9_]+): \$\{\{ secrets\.\1 \}\}$", workflow, re.MULTILINE))
    needed = set(missing_secrets(load_config(ROOT / "properties.toml"), {}))
    assert wired == needed  # adding a house to properties.toml means adding its secrets to the workflow


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
    for name in list(GOOD_ENV) + ["GITHUB_ACTIONS", "GITHUB_OUTPUT", "SITE_URL"]:
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
