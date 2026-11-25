"""The control panel: the lake, the makers' clouds, and what the page is given of them."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from casecal import devices, lake, lakealert, status, sync
from casecal.config import ClimateConfig, Config, ConfigError, LakeConfig, PropertyConfig, load_config
from casecal.crypto import open_envelope
from casecal.devices import AUTO, COOL, HEAT, Device, DeviceError
from casecal.errors import SyncAborted
from casecal.model import PropertySnapshot, Snapshot
from casecal.site import extract_envelope

NOW = datetime(2026, 11, 25, 10, 43, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def box(title, level, when="2026-11-25 11:00", icon="fa-water", unit="cm"):
    """One lake as laghi.net answers it: the series, and beside them their data in the same order."""
    names = [("Livello", True), ("Afflusso", False), ("Deflusso", False), ("Riempimento", False)]
    values = [level, "113.8", "-1", "26.5"]
    return {
        "tooltip_config": {"titolo": title, "icona_titolo": icon, "tooltip_type": 4,
                           "series": [{"nome": name, "misura_principale": main} for name, main in names]},
        "tooltip_data": [{"anagrafica_serie": {"UnMis": unit if main else "m3/s"}, "data_serie": [{"data": when, "val": value, "st": "001"}]}
                         for (name, main), value in zip(names, values)],
    }


ANSWER = {"success": True, "data": [box("Lago Maggiore", "0.1"), box("Lago di Como", "5.7", icon="fa-water-rise")]}


def test_the_lake_is_read_from_its_box_and_its_time_is_solar_time():
    reading = lake.parse_reading(ANSWER, "lago di  como")
    assert (reading.level_cm, reading.trend, reading.inflow, reading.fill) == (5.7, lake.RISING, 113.8, 26.5)
    assert reading.outflow is None  # a negative flow is the site's "not available"
    assert reading.read_at == datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)  # UTC+1, summer included


@pytest.mark.parametrize("answer, message", [
    ({"success": False}, "senza dati"),
    ({"success": True, "data": "x"}, "forma attesa"),
    ({"success": True, "data": [box("Lago d'Iseo", "1")]}, "non è tra quelli"),
    ({"success": True, "data": [box("Lago di Como", "N.D.")]}, "non riporta il livello"),
    ({"success": True, "data": [box("Lago di Como", "5", when="ieri")]}, "non riporta il livello"),
    ({"success": True, "data": [box("Lago di Como", "1.97", unit="m")]}, "centimetri"),
    ({"success": True, "data": [box("Lago di Como", "9000")]}, "plausibile"),
])
def test_an_answer_that_is_not_as_expected_is_a_lake_error(answer, message):
    with pytest.raises(lake.LakeError, match=message):
        lake.parse_reading(answer, "Lago di Como")


class Answer:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code, self._payload, self.text, self.headers = status_code, payload, text, headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class Web:
    """A session that answers from a script and remembers what it was asked."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def request(self, method, url, **kwargs):
        self.asked.append((method, url, kwargs))
        return self.answers.pop(0)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


def test_the_lake_is_asked_as_the_sites_own_page_asks():
    web = Web(Answer(text='<div class="MappaConSegnaposti"><input type="hidden" id="config" value="370"></div>'), Answer(payload=ANSWER))
    assert lake.fetch_reading("Lago di Como", session=web).level_cm == 5.7
    method, url, sent = web.asked[1]
    assert (method, url, sent["data"]) == ("POST", lake.DATA_URL, {"config": "370"})
    assert sent["headers"]["X-Requested-With"] == "XMLHttpRequest"
    with pytest.raises(lake.LakeError, match="HTTP 503"):
        lake.fetch_reading("Lago di Como", session=Web(Answer(text=""), Answer(503)))


def test_a_level_leaves_its_threshold_only_once_well_below_it():
    assert lake.stage(99.9, 100, 120) == lake.NORMAL
    assert lake.stage(100, 100, 120) == lake.ATTENTION
    assert lake.stage(120, 100, 120) == lake.ALARM
    assert lake.stage(97, 100, 120, before=lake.ATTENTION) == lake.ATTENTION  # hovering: not closed yet
    assert lake.stage(94, 100, 120, before=lake.ATTENTION) == lake.NORMAL
    assert lake.stage(117, 100, 120, before=lake.ALARM) == lake.ALARM
    assert lake.stage(110, 100, 120, before=lake.ALARM) == lake.ATTENTION


HOUSES = Config(site_id="test/site", properties=(
    PropertyConfig(id="santa", label="Santa", env="SANTA", wifi="SIM",
                   climate=(ClimateConfig("vaillant", "Riscaldamento"),)),
    PropertyConfig(id="vassena", label="Vassena", env="VASSENA", wifi="Eolo",
                   climate=(ClimateConfig("daikin", "Piano sopra"), ClimateConfig("airzone", "Piano sotto")),
                   lake=LakeConfig("Lago di Como", 100, 120)),
))
SECRETS = {name: "x" for name in devices.DEVICE_SECRETS}


def reading(level, at=NOW - timedelta(minutes=43)):
    return lake.LakeReading("Lago di Como", level, at, lake.STABLE)


def collect(units, level=5.5, previous=None, environ=SECRETS, now=NOW):
    def read(source, _):
        found = units[source]
        if isinstance(found, Exception):
            raise found
        return found

    def read_lake(_):
        if level is None:
            raise lake.LakeError("sito dei laghi non raggiungibile (Timeout)")
        return level if isinstance(level, lake.LakeReading) else reading(level)

    return status.collect_status(HOUSES, environ, previous=previous, now=now, read_devices=read, read_lake=read_lake)


def test_the_panel_says_what_each_app_said_and_judges_the_wifi_from_it():
    built, report = collect({
        "vaillant": [Device("Casa · Zona 1", True, True, HEAT, 19.5, 20.0)],
        "daikin": [Device("Sala", False), Device("Camera", False)],
        "airzone": [Device("A", True, True, COOL, 25, 24), Device("B", True, False, COOL, 27, 24), Device("C", None, None)],
    })
    santa, vassena = built["houses"]["santa"], built["houses"]["vassena"]
    assert santa["wifi"] == {"label": "SIM", "state": "online"}
    assert santa["climate"] == [{"label": "Riscaldamento", "app": "myVAILLANT", "state": "on", "units": 1, "running": 1,
                                 "mode": "heat", "room": 19.5, "target": 20.0}]
    assert vassena["wifi"]["state"] == "online"  # one unit seen is enough
    assert vassena["climate"][0]["state"] == "offline"
    assert (vassena["climate"][1]["state"], vassena["climate"][1]["units"], vassena["climate"][1]["running"]) == ("on", 2, 1)
    assert vassena["climate"][1]["room"] == 26.0
    assert (vassena["lake"]["level_cm"], vassena["lake"]["stage"]) == (5.5, "normal")
    assert sorted(report.read) == ["airzone", "daikin", "lago", "vaillant"] and not report.warnings


def test_a_source_that_fails_is_unknown_never_off_and_the_log_gets_no_detail():
    built, report = collect({
        "vaillant": DeviceError("myVAILLANT: accesso rifiutato (HTTP 401)"),
        "daikin": [Device("Sala", False)],
        "airzone": KeyError("Casa di Mario"),
    }, level=None)
    assert built["houses"]["santa"]["wifi"]["state"] == "unknown"
    assert built["houses"]["santa"]["climate"][0]["state"] == "unknown"
    assert built["houses"]["vassena"]["wifi"]["state"] == "offline"  # all that answered are out of reach
    assert "level_cm" not in built["houses"]["vassena"]["lake"]
    assert len(report.warnings) == 3 and not any("Mario" in note for note in report.warnings)


def test_a_source_without_its_secrets_is_off_and_is_not_asked():
    built, report = collect({"vaillant": AssertionError("asked"), "daikin": [], "airzone": []},
                            environ={k: v for k, v in SECRETS.items() if not k.startswith("VAILLANT")})
    assert report.off == ["vaillant"] and not report.warnings
    assert built["houses"]["santa"]["climate"][0]["state"] == "unknown"


def test_the_lakes_history_is_carried_on_and_an_old_reading_is_not_a_level():
    first, _ = collect({"vaillant": [], "daikin": [], "airzone": []}, level=98.0)
    later = NOW + timedelta(hours=6)
    second, _ = collect({"vaillant": [], "daikin": [], "airzone": []}, level=reading(101.0, later - timedelta(minutes=30)),
                        previous=first, now=later)
    held = second["houses"]["vassena"]["lake"]
    assert [level for _, level in held["history"]] == [98.0, 101.0] and held["stage"] == "attention"
    # The site keeps giving a reading of long ago: the level is unknown, where it stood is remembered.
    third, _ = collect({"vaillant": [], "daikin": [], "airzone": []}, level=reading(101.0, later - timedelta(minutes=30)),
                       previous=second, now=later + timedelta(hours=20))
    assert "level_cm" not in third["houses"]["vassena"]["lake"]
    assert third["houses"]["vassena"]["lake"]["stage_before"] == "attention"
    assert lakealert._stage(third["houses"]["vassena"]["lake"]) == "attention"


@pytest.mark.parametrize("now, before, issue_open, action", [
    (None, "alarm", True, "nothing"),
    ("normal", "normal", False, "nothing"),
    ("normal", "attention", True, "close"),
    ("attention", "normal", False, "open"),
    ("attention", "attention", True, "nothing"),
    ("alarm", "attention", True, "worse"),
    ("alarm", None, True, "nothing"),  # the page could not say where the lake stood: no message every hour
    ("alarm", "alarm", False, "open"),  # closed by hand while the lake is still high
])
def test_the_lake_alert_follows_the_level(now, before, issue_open, action):
    assert lakealert.decide(now, before, issue_open=issue_open) == action


class Issues:
    def __init__(self, open_issue=None):
        self.open_issue, self.done = open_issue, []

    def issue_titled(self, title, *, state, what):
        return self.open_issue

    def create_issue(self, title, body, *, what, assignees=None):
        self.done.append(("create", title, body, assignees))
        return 7

    def comment(self, number, body, *, what):
        self.done.append(("comment", number, body))

    def close_issue(self, number, *, what):
        self.done.append(("close", number))


def panel(level, stage):
    return {"houses": {"vassena": {"lake": {"name": "Lago di Como", "level_cm": level, "stage": stage,
                                            "attention_cm": 100.0, "alarm_cm": 120.0}}}}


def test_the_owner_is_written_to_when_the_lake_rises_and_the_issue_closes_when_it_falls():
    issues = Issues()
    assert lakealert.reconcile(issues, owner="owner", status=panel(104.5, "attention"), previous=panel(90, "normal")) == "open"
    (_, title, body, assignees), = issues.done
    assert title == lakealert.ISSUE_TITLE and assignees == ["owner"]
    assert "@owner" in body and "+104,5 cm" in body and "Vassena" not in body  # the level is public, the house is not
    issues = Issues({"number": 7})
    assert lakealert.reconcile(issues, owner="owner", status=panel(121, "alarm"), previous=panel(104.5, "attention")) == "worse"
    assert "ALLARME" in issues.done[0][2]
    issues = Issues({"number": 7})
    assert lakealert.reconcile(issues, owner="owner", status=panel(80, "normal"), previous=panel(121, "alarm")) == "close"
    assert [step[0] for step in issues.done] == ["comment", "close"]


def test_airzone_is_read_zone_by_zone():
    web = Web(
        Answer(payload={"token": "jwt", "refreshToken": "r"}),
        Answer(payload={"installations": [{"installation_id": "i1", "name": "Casa"}]}),
        Answer(payload={"groups": [{"devices": [{"device_id": "s1", "type": "az_system"},
                                                {"device_id": "z1", "type": "az_zone", "name": "Sala"}]}]}),
        Answer(payload={"power": True, "mode": 1, "auto_mode": 3, "isConnected": True, "ws_connected": True,
                        "local_temp": {"celsius": 20}, "setpoint_air_heat": {"celsius": 21.5}}),
    )
    assert devices.read_airzone({"AIRZONE_USER": "a@b.it ", "AIRZONE_PASS": "p"}, web) == [Device("Casa · Sala", True, True, HEAT, 20.0, 21.5)]
    assert web.asked[0][2]["json"] == {"email": "a@b.it", "password": "p"}
    assert web.asked[3][2]["headers"] == {"Authorization": "Bearer jwt"} and web.asked[3][2]["params"] == {"installation_id": "i1"}
    assert devices.airzone_device("x", {"power": False, "mode": 2, "isConnected": True, "ws_connected": False}).online is False
    with pytest.raises(DeviceError, match="accesso rifiutata"):
        devices.read_airzone({"AIRZONE_USER": "a", "AIRZONE_PASS": "p"}, Web(Answer(401, {})))


def test_daikin_units_are_read_from_their_climate_control_point():
    unit = {"id": "u1", "isCloudConnectionUp": {"value": True}, "managementPoints": [
        {"managementPointType": "gateway"},
        {"managementPointType": "climateControl", "name": {"value": "Camera"}, "onOffMode": {"value": "off"},
         "operationMode": {"value": "dry"}, "sensoryData": {"value": {"roomTemperature": {"value": 25.6}}},
         "temperatureControl": {"value": {"operationModes": {"cooling": {"setpoints": {"roomTemperature": {"value": 21}}}}}}}]}
    assert devices.daikin_device(unit) == Device("Camera", True, False, devices.DRY, 25.6, None)
    assert devices.daikin_device({"id": "u2"}) == Device("u2", None)


class Pass:
    def __init__(self, kept=None):
        self.kept, self.saved = kept, []

    def load(self):
        return self.kept

    def save(self, token):
        self.saved.append(token)


DAIKIN_ENV = {"DAIKIN_CLIENT_ID": "id", "DAIKIN_CLIENT_SECRET": "secret", "DAIKIN_REFRESH_TOKEN": "first"}


def test_daikins_new_pass_is_kept_before_anything_else_and_the_secret_is_the_fallback():
    web = Web(Answer(payload={"access_token": "a", "refresh_token": "third"}), Answer(payload=[]))
    store = Pass("second")
    assert devices.read_daikin(DAIKIN_ENV, web, store) == []
    assert web.asked[0][2]["params"]["refresh_token"] == "second" and store.saved == ["third"]
    # The kept pass is void (the issue was restored, say): the secret starts the chain again.
    web = Web(Answer(400, {}), Answer(payload={"access_token": "a", "refresh_token": "new"}), Answer(payload=[]))
    store = Pass("stale")
    devices.read_daikin(DAIKIN_ENV, web, store)
    assert [asked[2]["params"]["refresh_token"] for asked in web.asked[:2]] == ["stale", "first"] and store.saved == ["new"]
    with pytest.raises(DeviceError, match="daikin-login"):
        devices.read_daikin(DAIKIN_ENV, Web(Answer(400, {})), Pass())


def test_the_pass_is_kept_encrypted_in_an_issue(monkeypatch):
    from casecal import passes

    class Kept(Issues):
        def create_issue(self, title, body, *, what, assignees=None):
            self.open_issue = {"number": 3, "body": body}
            return 3

        def set_issue_body(self, number, body, *, what):
            self.open_issue["body"] = body

    issues, key = Kept(), b"k" * 32
    store = passes.IssuePass(issues, key)
    assert store.load() is None
    store.save("token-one")
    assert "token-one" not in issues.open_issue["body"]
    again = passes.IssuePass(issues, key)
    assert again.load() == "token-one"
    again.save("token-two")
    assert passes.IssuePass(issues, key).load() == "token-two"
    assert passes.IssuePass(issues, b"z" * 32).load() is None  # another password: start again from the secret


def test_the_vaillant_proof_of_work_is_solved_as_the_login_page_checks_it():
    import base64
    import hashlib

    challenge = {"parameters": {"algorithm": "PBKDF2/SHA-256", "cost": 50, "keyLength": 32, "keyPrefix": "00",
                                "nonce": "aa" * 16, "salt": "bb" * 16}, "signature": "cc"}
    solved = json.loads(base64.b64decode(devices.solve_proof(challenge)))
    counter = solved["solution"]["counter"]
    derived = hashlib.pbkdf2_hmac("sha256", bytes.fromhex("aa" * 16) + counter.to_bytes(4, "big"), bytes.fromhex("bb" * 16), 50, dklen=32)
    assert derived[0] == 0 and solved["solution"]["derivedKey"] == derived.hex() and solved["challenge"] == challenge
    with pytest.raises(DeviceError, match="prova di lavoro"):
        devices.solve_proof({"parameters": {}})


def test_vaillant_signs_in_as_the_app_does_and_reads_the_zones():
    realm = "https://identity.vaillant-group.com/auth/realms/vaillant-italy-b2c"
    form = f'<form action="{realm}/login-actions/authenticate?session_code=abc&amp;tab_id=1" method="post">'
    challenge = {"parameters": {"algorithm": "PBKDF2/SHA-256", "cost": 10, "keyLength": 32, "keyPrefix": "00",
                                "nonce": "01", "salt": "02"}, "signature": "s"}
    system = {
        "state": {"zones": [{"index": 0, "desiredRoomTemperatureSetpoint": 18.5, "currentRoomTemperature": 17.94}],
                  "circuits": [{"index": 0, "circuitState": "HEATING"}]},
        "properties": {"zones": [{"index": 0, "isActive": True, "associatedCircuitIndex": 0}, {"index": 1, "isActive": False}]},
        "configuration": {"zones": [{"index": 0, "general": {"name": "Giorno"}, "heating": {"operationModeHeating": "TIME_CONTROLLED"}},
                                    {"index": 1, "general": {"name": "Spenta"}, "heating": {"operationModeHeating": "OFF"}}]},
    }
    web = Web(
        Answer(text=form), Answer(payload=challenge),
        Answer(302, headers={"Location": "enduservaillant.page.link://login?session_state=x&code=the-code"}),
        Answer(payload={"access_token": "token"}),
        Answer(payload=[{"systemId": "sys/1", "homeName": "Casa"}, {"homeName": "senza impianto"}]),
        Answer(payload={"connected": True}), Answer(payload={"controlIdentifier": "tli"}), Answer(payload=system),
    )
    found = devices.read_vaillant({"VAILLANT_USER": "u", "VAILLANT_PASS": "p"}, web)
    assert found == [Device("Casa · Giorno", True, True, HEAT, 17.9, 18.5)]
    assert web.asked[2][1] == f"{realm}/login-actions/authenticate?session_code=abc&tab_id=1"
    assert web.asked[2][2]["data"]["username"] == "u" and web.asked[2][2]["data"]["altcha"]
    assert web.asked[3][2]["data"]["code"] == "the-code"
    assert web.asked[7][1].endswith("/end-user-app-api/v1/systems/sys%2F1/tli")
    # A control of a kind not known: the connection is still told, the heating is not guessed.
    assert devices.vaillant_devices("Casa", False, None) == [Device("Casa", False)]
    off = dict(system, configuration={"zones": [{"index": 0, "heating": {"operationModeHeating": "OFF"}}]})
    assert devices.vaillant_devices("Casa", True, off)[0].on is False
    with pytest.raises(DeviceError, match="accesso rifiutato"):
        devices.read_vaillant({"VAILLANT_USER": "u", "VAILLANT_PASS": "p"}, Web(Answer(text=form), Answer(payload=challenge), Answer(200, text="no")))


def test_the_panel_is_set_in_the_configuration(tmp_path):
    config = load_config(ROOT / "properties.toml")
    assert [entry.source for prop in config.properties for entry in prop.climate] == ["vaillant", "daikin", "airzone"]
    assert config.properties[1].lake == LakeConfig("Lago di Como", 100.0, 120.0) and config.properties[0].lake is None
    base = 'title = "x"\n[[property]]\nid = "a"\nlabel = "A"\nenv = "A"\n'
    for extra, message in [
        ('climate = [{ source = "nest", label = "x" }]', "source deve essere"),
        ('climate = [{ source = "daikin" }]', "label"),
        ('lake = { name = "Lago", attention_cm = 120, alarm_cm = 100 }', "più bassa"),
        ('lake = { name = "Lago", attention_cm = "alta", alarm_cm = 100 }', "centimetri"),
        ("wifi = 3", "wifi"),
    ]:
        path = tmp_path / "p.toml"
        path.write_text(base + extra + "\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=message):
            load_config(path)


FAMILY = "nebbia-quercia-sentiero-42"


def test_between_nights_only_the_panel_of_the_page_online_changes(tmp_path, monkeypatch):
    snapshot = Snapshot(generated_at=NOW - timedelta(hours=8), properties=[], title="Calendario case",
                        status={"checked_at": "2026-11-25T02:17:00Z", "houses": {}})
    online = sync.publish(snapshot, passphrase=FAMILY, site_id="test/site", out_dir=tmp_path / "online")
    envelope = extract_envelope(online.read_text(encoding="utf-8"))
    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: envelope if url == "https://site" else None)
    seen = {}

    def collect_panel(config, environ, *, previous, now):
        seen["previous"] = previous
        return {"checked_at": "2026-11-25T10:43:00Z", "houses": {"vassena": {}}}, status.StatusReport(read=["lago"])

    page, report, built, previous = sync.refresh_status(
        HOUSES, {}, site_url="https://site", passphrase=FAMILY, out_dir=tmp_path / "out", now=NOW, collect=collect_panel)
    family, owner = open_envelope(extract_envelope(page.read_text(encoding="utf-8")), passphrase=FAMILY)
    before, owner_before = open_envelope(envelope, passphrase=FAMILY)
    assert family["status"] == built and seen["previous"] == before["status"] == previous
    assert {k: v for k, v in family.items() if k != "status"} == {k: v for k, v in before.items() if k != "status"}
    assert owner == owner_before and report.read == ["lago"]
    for url, passphrase in [(None, FAMILY), ("https://elsewhere", FAMILY), ("https://site", "un'altra-password-lunga-9")]:
        with pytest.raises(SyncAborted):
            sync.refresh_status(HOUSES, {}, site_url=url, passphrase=passphrase, out_dir=tmp_path / "no", now=NOW, collect=collect_panel)
    assert not (tmp_path / "no").exists()
