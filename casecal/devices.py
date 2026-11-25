"""Heating and cooling, read from the clouds behind the apps the family uses.

Three makers, three services, one shape: each reader returns the units of one
account as ``Device``s, saying of each whether its cloud can see it (which is
also how the house's WiFi is judged), whether it is on, and in which mode.

* **Airzone Cloud**: the app's own web service; e-mail and password.
* **Vaillant myVAILLANT**: the app's own service, behind the login page of the
  app (which asks for a small proof of work before the password); e-mail and
  password. Not a published interface: it is what the app does.
* **Daikin Onecta**: Daikin's published interface. It works on a pass that is
  renewed at every use, and each renewal voids the pass before it: the newest
  one has to be kept between runs (see ``DaikinPass``).

Only reading is ever done: nothing here can switch a unit on or off.

Every message of ``DeviceError`` is written for a public log: it says what
went wrong in kind, and never quotes an address, a name or an answer.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import requests

TIMEOUT_SECONDS = 30
HEAT, COOL, AUTO, DRY, FAN = "heat", "cool", "auto", "dry", "fan"


class DeviceError(RuntimeError):
    """A maker's cloud could not be read. The message is safe for a public log."""


@dataclass(frozen=True)
class Device:
    name: str
    online: bool | None  # whether its cloud sees it; None: the cloud does not say
    on: bool | None = None
    mode: str | None = None  # HEAT, COOL, AUTO, DRY, FAN
    room: float | None = None  # °C
    target: float | None = None  # °C


def _temperature(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return round(float(raw), 1) if -40 <= raw <= 60 else None


def _ask(session: requests.Session, method: str, url: str, who: str, **kwargs: Any) -> requests.Response:
    try:
        return session.request(method, url, timeout=TIMEOUT_SECONDS, **kwargs)
    except requests.RequestException as exc:
        raise DeviceError(f"{who} non raggiungibile ({type(exc).__name__})") from None


def _json(response: requests.Response, who: str, what: str) -> Any:
    status = response.status_code
    if status in (401, 403):
        raise DeviceError(f"{who}: {what} rifiutata (HTTP {status})")
    if status == 429:
        raise DeviceError(f"{who}: troppe richieste, riprovare più tardi")
    if status >= 400:
        raise DeviceError(f"{who}: {what} fallita (HTTP {status})")
    try:
        return response.json()
    except ValueError:
        raise DeviceError(f"{who}: {what}, risposta inattesa") from None


# ---------------------------------------------------------------- Airzone Cloud

AIRZONE_API = "https://m.airzonecloud.com/api/v1"
_AIRZONE = "Airzone Cloud"
_AIRZONE_HEAT, _AIRZONE_COOL = {3, 6, 7, 8, 9}, {2, 10, 11, 12}
_AIRZONE_MODES = {1: AUTO, 4: FAN, 5: DRY}
_AIRZONE_SETPOINTS = {HEAT: "setpoint_air_heat", COOL: "setpoint_air_cool", AUTO: "setpoint_air_auto",
                      DRY: "setpoint_air_dry", FAN: "setpoint_air_vent"}
_AIRZONE_UNITS = ("az_zone", "aidoo", "aidoo_it")  # what has a power of its own; a system has none
AIRZONE_MAX_UNITS = 40  # the service answers "too many requests" to a burst


def _airzone_mode(code: Any) -> str | None:
    if code in _AIRZONE_HEAT:
        return HEAT
    if code in _AIRZONE_COOL:
        return COOL
    return _AIRZONE_MODES.get(code)


def airzone_device(name: str, status: Mapping[str, Any]) -> Device:
    """One zone, from what the service says of it."""
    seen = [status.get(key) for key in ("isConnected", "ws_connected") if isinstance(status.get(key), bool)]
    mode = _airzone_mode(status.get("mode"))
    if mode == AUTO and _airzone_mode(status.get("auto_mode")) in (HEAT, COOL):
        mode = _airzone_mode(status.get("auto_mode"))

    def celsius(key: str | None) -> float | None:
        held = status.get(key) if key else None
        return _temperature(held.get("celsius")) if isinstance(held, Mapping) else None

    power = status.get("power")
    return Device(
        name=name,
        online=all(seen) if seen else None,
        on=power if isinstance(power, bool) else None,
        mode=mode,
        room=celsius("local_temp"),
        target=celsius(_AIRZONE_SETPOINTS.get(mode or "")),
    )


def read_airzone(environ: Mapping[str, str], session: requests.Session | None = None) -> list[Device]:
    session = session or requests.Session()
    login = _json(
        _ask(session, "POST", f"{AIRZONE_API}/auth/login", _AIRZONE,
             json={"email": environ["AIRZONE_USER"].strip(), "password": environ["AIRZONE_PASS"]}),
        _AIRZONE, "accesso",
    )
    token = login.get("token") if isinstance(login, Mapping) else None
    if not isinstance(token, str) or not token:
        raise DeviceError(f"{_AIRZONE}: accesso, risposta inattesa")
    headers = {"Authorization": f"Bearer {token}"}

    def get(path: str, what: str, **params: str) -> Any:
        return _json(_ask(session, "GET", AIRZONE_API + path, _AIRZONE, headers=headers, params=params), _AIRZONE, what)

    devices: list[Device] = []
    try:
        listed = get("/installations", "elenco degli impianti")
        for installation in listed["installations"]:
            where = str(installation["installation_id"])
            detail = get(f"/installations/{quote(where, safe='')}", "impianto")
            for group in detail.get("groups") or []:
                for unit in group.get("devices") or []:
                    if unit.get("type") not in _AIRZONE_UNITS:
                        continue
                    if len(devices) == AIRZONE_MAX_UNITS:
                        raise DeviceError(f"{_AIRZONE}: più di {AIRZONE_MAX_UNITS} zone")
                    status = get(f"/devices/{quote(str(unit['device_id']), safe='')}/status", "stato di una zona",
                                 installation_id=where)
                    if not isinstance(status, Mapping):
                        raise DeviceError(f"{_AIRZONE}: stato di una zona, risposta inattesa")
                    name = " · ".join(str(part) for part in (installation.get("name"), unit.get("name")) if part)
                    devices.append(airzone_device(name, status))
    except (KeyError, TypeError, AttributeError):
        raise DeviceError(f"{_AIRZONE}: risposta inattesa") from None
    return devices


# ---------------------------------------------------------------- Vaillant myVAILLANT

VAILLANT_LOGIN = "https://identity.vaillant-group.com/auth/realms/{realm}"
VAILLANT_PROOF = "https://identity.vaillant-group.com/api/altcha/challenge"
VAILLANT_API = "https://api.vaillant-group.com/service-connected-control"
_VAILLANT = "myVAILLANT"
_VAILLANT_CLIENT, _VAILLANT_RETURN = "myvaillant", "enduservaillant.page.link://login"
_VAILLANT_HEADERS = {
    "x-app-identifier": "VAILLANT",
    "Accept-Language": "en-GB",
    "Accept": "application/json, text/plain, */*",
    "x-client-locale": "en-GB",
    "x-idm-identifier": "KEYCLOAK",
    "ocp-apim-subscription-key": "1e0a2f3511fb4c5bbb1c7f9fedd20b1c",  # the app's own, the same for everyone
    "User-Agent": "okhttp/4.9.2",
}
_COUNTRY_RE = re.compile(r"^[a-z]{3,30}$")
PROOF_MAX_TRIES = 100_000  # a prefix of one byte takes some 256


def solve_proof(challenge: Mapping[str, Any]) -> str:
    """The small proof of work the login page asks for before it takes a password.

    A key is derived from the page's nonce followed by a counter, for one
    counter after another, until it begins with the prefix the page asked for.
    """
    try:
        parameters = challenge["parameters"]
        algorithm = str(parameters["algorithm"])
        digest = {"PBKDF2/SHA-512": "sha512", "PBKDF2/SHA-384": "sha384"}.get(algorithm, "sha256")
        nonce, salt = bytes.fromhex(parameters["nonce"]), bytes.fromhex(parameters["salt"])
        prefix = bytes.fromhex(parameters["keyPrefix"])
        cost, length = int(parameters["cost"]), int(parameters["keyLength"])
        signature = challenge["signature"]
    except (KeyError, TypeError, ValueError):
        raise DeviceError(f"{_VAILLANT}: la pagina di accesso è cambiata (prova di lavoro)") from None
    if not (1 <= cost <= 50_000 and 1 <= length <= 64 and len(prefix) <= 1):
        raise DeviceError(f"{_VAILLANT}: prova di lavoro troppo onerosa")
    for counter in range(PROOF_MAX_TRIES):
        derived = hashlib.pbkdf2_hmac(digest, nonce + counter.to_bytes(4, "big"), salt, cost, dklen=length)
        if derived.startswith(prefix):
            solved = {
                "challenge": {"parameters": parameters, "signature": signature},
                "solution": {"counter": counter, "derivedKey": derived.hex(), "time": 0},
            }
            return base64.b64encode(json.dumps(solved, separators=(",", ":")).encode()).decode()
    raise DeviceError(f"{_VAILLANT}: prova di lavoro non risolta")


def _code_from(response: requests.Response) -> str | None:
    found = parse_qs(urlsplit(response.headers.get("Location", "")).query).get("code")
    return found[0] if found else None


def vaillant_token(environ: Mapping[str, str], session: requests.Session) -> str:
    """Sign in as the app does: the login page, its proof of work, the password, then the pass."""
    country = (environ.get("VAILLANT_COUNTRY", "").strip() or "italy").lower()
    if not _COUNTRY_RE.match(country):
        raise DeviceError("VAILLANT_COUNTRY deve essere il nome inglese di un paese, es. italy")
    base = VAILLANT_LOGIN.format(realm=f"vaillant-{country}-b2c")
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    verifier = "".join(secrets.choice(alphabet) for _ in range(128))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    page = _ask(session, "GET", f"{base}/protocol/openid-connect/auth", _VAILLANT, allow_redirects=False, params={
        "response_type": "code", "client_id": _VAILLANT_CLIENT, "code": "code_challenge",
        "redirect_uri": _VAILLANT_RETURN, "code_challenge_method": "S256", "code_challenge": challenge,
    })
    code = _code_from(page)
    if code is None:
        form = re.search(re.escape(f"{base}/login-actions/authenticate") + r'\?[^"]*', page.text or "")
        if page.status_code != 200 or not form:
            raise DeviceError(f"{_VAILLANT}: pagina di accesso non trovata (HTTP {page.status_code})")
        proof = solve_proof(_json(_ask(session, "GET", VAILLANT_PROOF, _VAILLANT), _VAILLANT, "prova di lavoro"))
        sent = _ask(session, "POST", html.unescape(form.group()), _VAILLANT, allow_redirects=False, data={
            "username": environ["VAILLANT_USER"].strip(), "password": environ["VAILLANT_PASS"],
            "credentialId": "", "altcha": proof,
        })
        code = _code_from(sent)
        if code is None:
            raise DeviceError(f"{_VAILLANT}: accesso rifiutato (credenziali errate, o pagina di accesso cambiata)")
    granted = _json(
        _ask(session, "POST", f"{base}/protocol/openid-connect/token", _VAILLANT, data={
            "grant_type": "authorization_code", "client_id": _VAILLANT_CLIENT, "code": code,
            "code_verifier": verifier, "redirect_uri": _VAILLANT_RETURN,
        }),
        _VAILLANT, "accesso",
    )
    token = granted.get("access_token") if isinstance(granted, Mapping) else None
    if not isinstance(token, str) or not token:
        raise DeviceError(f"{_VAILLANT}: accesso, risposta inattesa")
    return token


def _by_index(block: Any, kind: str) -> dict[Any, Mapping[str, Any]]:
    items = block.get(kind) if isinstance(block, Mapping) else None
    return {item.get("index"): item for item in items or [] if isinstance(item, Mapping)}


def vaillant_devices(home: str, connected: bool | None, system: Mapping[str, Any] | None) -> list[Device]:
    """The zones of one house's heating. Without the system's state, the house alone, for its connection."""
    if not isinstance(system, Mapping):
        return [Device(name=home, online=connected)]
    state, properties, configuration = (system.get(part) or {} for part in ("state", "properties", "configuration"))
    whole = state.get("system") if isinstance(state.get("system"), Mapping) else {}
    zones, about = _by_index(state, "zones"), _by_index(properties, "zones")
    circuits = _by_index(state, "circuits")
    devices: list[Device] = []
    for index, setting in _by_index(configuration, "zones").items():
        if about.get(index, {}).get("isActive") is False:
            continue
        heating = setting.get("heating") if isinstance(setting.get("heating"), Mapping) else {}
        zone = zones.get(index, {})
        circuit = circuits.get(about.get(index, {}).get("associatedCircuitIndex"), {})
        mode = heating.get("operationModeHeating")
        on: bool | None = None
        if isinstance(mode, str):
            # Off is off; anything else is the heating left to its thermostat or its hours.
            on = not (mode == "OFF" or whole.get("systemOff") is True or zone.get("currentSpecialFunction") == "SYSTEM_OFF")
        general = setting.get("general") if isinstance(setting.get("general"), Mapping) else {}
        devices.append(Device(
            name=" · ".join(str(part) for part in (home, general.get("name")) if part),
            online=connected,
            on=on,
            mode=COOL if circuit.get("circuitState") == "COOLING" else HEAT,
            room=_temperature(zone.get("currentRoomTemperature")),
            target=_temperature(zone.get("desiredRoomTemperatureSetpoint")),
        ))
    return devices or [Device(name=home, online=connected)]


def read_vaillant(environ: Mapping[str, str], session: requests.Session | None = None) -> list[Device]:
    session = session or requests.Session()
    headers = {**_VAILLANT_HEADERS, "Authorization": f"Bearer {vaillant_token(environ, session)}"}
    app = f"{VAILLANT_API}/end-user-app-api/v1"

    def get(url: str, what: str) -> Any:
        return _json(_ask(session, "GET", url, _VAILLANT, headers=headers), _VAILLANT, what)

    homes = get(f"{app}/homes", "elenco delle case")
    if not isinstance(homes, list):
        raise DeviceError(f"{_VAILLANT}: elenco delle case, risposta inattesa")
    devices: list[Device] = []
    for home in homes[:4]:
        if not isinstance(home, Mapping) or not home.get("systemId"):
            continue
        system_id = quote(str(home["systemId"]), safe="")
        name = str(home.get("homeName") or "")
        link = get(f"{app}/systems/{system_id}/meta-info/connection-status", "stato della connessione")
        connected = link.get("connected") is True if isinstance(link, Mapping) else None
        control = get(f"{app}/systems/{system_id}/meta-info/control-identifier", "tipo di centralina")
        kind = control.get("controlIdentifier") if isinstance(control, Mapping) else None
        system = None
        if kind == "tli":
            system = get(f"{app}/systems/{system_id}/tli", "stato dell'impianto")
        elif kind == "vrc700":
            system = get(f"{VAILLANT_API}/vrc700/v1/systems/{system_id}", "stato dell'impianto")
        # Any other kind of control: the connection is still known, the heating is not.
        devices.extend(vaillant_devices(name, connected, system))
    return devices


# ---------------------------------------------------------------- Daikin Onecta

DAIKIN_AUTHORIZE = "https://idp.onecta.daikineurope.com/v1/oidc/authorize"
DAIKIN_TOKEN = "https://idp.onecta.daikineurope.com/v1/oidc/token"
DAIKIN_DEVICES = "https://api.onecta.daikineurope.com/v1/gateway-devices"
DAIKIN_SCOPE = "openid onecta:basic.integration offline_access"
_DAIKIN = "Daikin Onecta"
_DAIKIN_MODES = {"heating": HEAT, "heatingDay": HEAT, "heatingNight": HEAT, "cooling": COOL,
                 "auto": AUTO, "dry": DRY, "fanOnly": FAN}


class DaikinPass(Protocol):
    """Where the pass Daikin renews at every use is kept between one run and the next."""

    def load(self) -> str | None: ...

    def save(self, refresh_token: str) -> None: ...


def daikin_authorize_url(client_id: str, redirect_uri: str) -> str:
    """The page where the owner allows this program to read their units, once."""
    return DAIKIN_AUTHORIZE + "?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": DAIKIN_SCOPE,
    })


def daikin_grant(session: requests.Session, client_id: str, client_secret: str, **grant: str) -> Mapping[str, Any]:
    answer = _ask(session, "POST", DAIKIN_TOKEN, _DAIKIN,
                  params={"client_id": client_id, "client_secret": client_secret, **grant})
    if answer.status_code == 400:
        raise DeviceError(
            f"{_DAIKIN}: autorizzazione scaduta o già usata: va rifatta (python -m casecal daikin-login) "
            "e DAIKIN_REFRESH_TOKEN aggiornato"
        )
    granted = _json(answer, _DAIKIN, "autorizzazione")
    if not isinstance(granted, Mapping) or not granted.get("access_token") or not granted.get("refresh_token"):
        raise DeviceError(f"{_DAIKIN}: autorizzazione, risposta inattesa")
    return granted


def daikin_device(raw: Mapping[str, Any]) -> Device:
    link = raw.get("isCloudConnectionUp")
    online = link.get("value") if isinstance(link, Mapping) and isinstance(link.get("value"), bool) else None
    points = [point for point in raw.get("managementPoints") or [] if isinstance(point, Mapping)]
    climate = next((point for point in points if point.get("managementPointType") == "climateControl"), {})

    def value(*path: str) -> Any:
        held: Any = climate
        for step in path:
            held = held.get(step) if isinstance(held, Mapping) else None
        return held

    power, mode = value("onOffMode", "value"), value("operationMode", "value")
    return Device(
        name=str(value("name", "value") or raw.get("id") or ""),
        online=online,
        on={"on": True, "off": False}.get(power),
        mode=_DAIKIN_MODES.get(mode),
        room=_temperature(value("sensoryData", "value", "roomTemperature", "value")),
        target=_temperature(value("temperatureControl", "value", "operationModes", str(mode), "setpoints",
                                  "roomTemperature", "value")),
    )


def read_daikin(
    environ: Mapping[str, str],
    session: requests.Session | None = None,
    store: DaikinPass | None = None,
) -> list[Device]:
    session = session or requests.Session()
    if store is None:
        from .passes import issue_pass  # late: only this reader keeps anything between runs

        store = issue_pass(environ)
    client_id, client_secret = environ["DAIKIN_CLIENT_ID"].strip(), environ["DAIKIN_CLIENT_SECRET"].strip()
    # The pass kept by the last run; the secret is the first of the chain, or a new start.
    kept, given = store.load(), environ["DAIKIN_REFRESH_TOKEN"].strip()
    granted = None
    for candidate in dict.fromkeys(token for token in (kept, given) if token):
        try:
            granted = daikin_grant(session, client_id, client_secret, grant_type="refresh_token", refresh_token=candidate)
            break
        except DeviceError:
            if candidate == given:
                raise
    assert granted is not None
    # Kept before anything else is done: the pass just used is void from now on.
    store.save(str(granted["refresh_token"]))
    answer = _ask(session, "GET", DAIKIN_DEVICES, _DAIKIN,
                  headers={"Authorization": f"Bearer {granted['access_token']}", "Accept-Encoding": "gzip"})
    units = _json(answer, _DAIKIN, "elenco delle unità")
    if not isinstance(units, list):
        raise DeviceError(f"{_DAIKIN}: elenco delle unità, risposta inattesa")
    return [daikin_device(unit) for unit in units if isinstance(unit, Mapping)]


# ---------------------------------------------------------------- the sources a house may name


@dataclass(frozen=True)
class Source:
    label: str  # as the page names the app
    secrets: tuple[str, ...]  # all needed; without them the source is simply off
    read: Callable[[Mapping[str, str]], Sequence[Device]]


SOURCES: dict[str, Source] = {
    "airzone": Source("Airzone Cloud", ("AIRZONE_USER", "AIRZONE_PASS"), read_airzone),
    "vaillant": Source("myVAILLANT", ("VAILLANT_USER", "VAILLANT_PASS"), read_vaillant),
    "daikin": Source("Daikin Onecta", ("DAIKIN_CLIENT_ID", "DAIKIN_CLIENT_SECRET", "DAIKIN_REFRESH_TOKEN"), read_daikin),
}
DEVICE_SECRETS = tuple(name for source in SOURCES.values() for name in source.secrets) + ("VAILLANT_COUNTRY",)
