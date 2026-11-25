"""How high the lake is, from the regulators' public page (laghi.net).

The house on the lake keeps a boat at its mooring: when the lake rises the
boat is at risk, and someone has to go and see to it. The level is public data,
published by the bodies that regulate the great lakes; the page that shows it
draws a box per lake from one request, and this module makes the same request.

The level is the *altezza idrometrica*: centimetres above the lake's own zero
(for Lake Como, the gauge of Malgrate). The site gives its times in "ora
solare", UTC+1 all year round.

That request is not a documented service: it is what the site's own page does.
Whatever is not as expected is a ``LakeError``, and the panel then says that
the level could not be read; nothing here can stop the calendar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import requests

PAGE_URL = "https://laghi.net/Page/laghi"
DATA_URL = "https://laghi.net/MappaConSegnaposti/BootWidgetData"
SOURCE = "laghi.net"
TIMEOUT_SECONDS = 30
SITE_TIME = timezone(timedelta(hours=1))  # "ora solare (UTC+1)", summer included

RISING, STABLE, FALLING = "rising", "stable", "falling"
# The picture the site puts beside the level says which way it is going.
_TRENDS = {"fa-water-rise": RISING, "fa-water": STABLE, "fa-water-arrow-down": FALLING}
# The widget's own setting, where the page carries one (a field inside the map's box).
_CONFIG_RE = re.compile(
    r'<input\b(?=[^>]*\bid="config")[^>]*\bvalue="([^"]*)"', re.IGNORECASE
)
_USER_AGENT = "Mozilla/5.0 (compatible; casecal)"

NORMAL, ATTENTION, ALARM = "normal", "attention", "alarm"
# A reading older than this says nothing about the lake as it is now.
MAX_AGE = timedelta(hours=12)
HISTORY_KEPT = timedelta(hours=72)
# Coming down, a level is left only once the lake is this far below its threshold: a lake
# that hovers around one must not open and close the alert at every reading.
HYSTERESIS_CM = 5.0


class LakeError(RuntimeError):
    """The level could not be read. The message is safe for a public log."""


@dataclass(frozen=True)
class LakeReading:
    name: str
    level_cm: float
    read_at: datetime  # aware, UTC
    trend: str | None = None  # RISING, STABLE, FALLING as the site judges it
    inflow: float | None = None  # m³/s
    outflow: float | None = None  # m³/s
    fill: float | None = None  # percent of the regulated volume


def _number(raw: Any) -> float | None:
    """A measure as the site writes it; ``None`` for a blank, a dash or "N.D."."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str):
        return None
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError:
        return None


def _moment(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        local = datetime.strptime(raw.strip()[:16].replace("T", " "), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return local.replace(tzinfo=SITE_TIME).astimezone(timezone.utc)


def _latest(series: Any) -> Mapping[str, Any]:
    """The newest point of one of a lake's series, or an empty mapping."""
    try:
        point = series["data_serie"][0]
    except (KeyError, IndexError, TypeError):
        return {}
    return point if isinstance(point, Mapping) else {}


def parse_reading(payload: Any, name: str) -> LakeReading:
    """The box of the lake called ``name`` in what the site answered.

    Each box has a list of series (the level, marked as the main one, then
    inflow, outflow and how full the lake is) and, beside it, the data of each
    in the same order.
    """
    if not isinstance(payload, Mapping) or not payload.get("success"):
        raise LakeError("il sito dei laghi ha risposto senza dati")
    boxes = payload.get("data")
    if not isinstance(boxes, Sequence) or isinstance(boxes, (str, bytes)):
        raise LakeError("la risposta del sito dei laghi non ha la forma attesa")
    wanted = " ".join(name.casefold().split())
    for box in boxes:
        if not isinstance(box, Mapping):
            continue
        config, data = box.get("tooltip_config"), box.get("tooltip_data")
        if not isinstance(config, Mapping) or not isinstance(data, Sequence):
            continue
        if " ".join(str(config.get("titolo", "")).casefold().split()) != wanted:
            continue
        series = config.get("series")
        if not isinstance(series, Sequence):
            break
        level = moment = None
        others: dict[str, float | None] = {}
        for index, serie in enumerate(series):
            if not isinstance(serie, Mapping) or index >= len(data):
                continue
            point = _latest(data[index])
            if serie.get("misura_principale"):
                unit = ""
                if isinstance(data[index], Mapping) and isinstance(data[index].get("anagrafica_serie"), Mapping):
                    unit = str(data[index]["anagrafica_serie"].get("UnMis") or "")
                if unit.strip().lower() not in ("cm", ""):
                    raise LakeError("il livello del lago non è più dato in centimetri")
                level, moment = _number(point.get("val")), _moment(point.get("data"))
            else:
                others[str(serie.get("nome", "")).strip().casefold()] = _number(point.get("val"))
        if level is None or moment is None:
            raise LakeError("il sito dei laghi non riporta il livello in questo momento")
        if not -300 <= level <= 600:
            raise LakeError("il livello letto non è plausibile")

        def flow(label: str) -> float | None:
            value = others.get(label)
            return value if value is not None and value >= 0 else None  # a negative flow is the site's "N.D."

        return LakeReading(
            name=str(config.get("titolo", name)).strip(),
            level_cm=level,
            read_at=moment,
            trend=_TRENDS.get(str(config.get("icona_titolo", ""))),
            inflow=flow("afflusso"),
            outflow=flow("deflusso"),
            fill=others.get("riempimento"),
        )
    raise LakeError("il lago cercato non è tra quelli del sito")


def fetch_reading(name: str, *, session: requests.Session | None = None) -> LakeReading:
    """Ask the site as its own page does: open the page, then request the data of its map."""
    session = session or requests.Session()
    headers = {"User-Agent": _USER_AGENT, "Accept-Language": "it"}
    try:
        page = session.get(PAGE_URL, headers=headers, timeout=TIMEOUT_SECONDS)
        if page.status_code != 200:
            raise LakeError(f"il sito dei laghi ha risposto HTTP {page.status_code}")
        found = _CONFIG_RE.search(page.text)
        answer = session.post(
            DATA_URL,
            data={"config": found.group(1)} if found else {},
            headers={**headers, "X-Requested-With": "XMLHttpRequest", "Referer": PAGE_URL, "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise LakeError(f"sito dei laghi non raggiungibile ({type(exc).__name__})") from None
    if answer.status_code != 200:
        raise LakeError(f"il sito dei laghi ha risposto HTTP {answer.status_code}")
    try:
        payload = answer.json()
    except ValueError:
        raise LakeError("la risposta del sito dei laghi non ha la forma attesa") from None
    return parse_reading(payload, name)


def stage(level_cm: float, attention_cm: float, alarm_cm: float, before: str | None = None) -> str:
    """Where the level stands against the two thresholds, given where it stood."""
    ease = HYSTERESIS_CM
    if level_cm >= alarm_cm or (before == ALARM and level_cm > alarm_cm - ease):
        return ALARM
    if level_cm >= attention_cm or (before in (ATTENTION, ALARM) and level_cm > attention_cm - ease):
        return ATTENTION
    return NORMAL


def extend_history(
    history: Sequence[Sequence[Any]] | None, reading: LakeReading | None, now: datetime
) -> list[list[Any]]:
    """The readings of the last days, oldest first, with tonight's added: ``[iso time, cm]`` each.

    The page tells from them how fast the lake is moving; they are kept in the
    page itself, the job's only memory.
    """
    from .model import iso_utc, parse_utc  # late: model knows nothing of lakes

    kept: dict[datetime, float] = {}
    for item in history or []:
        try:
            moment, level = parse_utc(item[0]), float(item[1])
        except (IndexError, TypeError, ValueError):
            continue
        if now - moment <= HISTORY_KEPT:
            kept[moment] = level
    if reading is not None and now - reading.read_at <= HISTORY_KEPT:
        kept[reading.read_at] = reading.level_cm
    return [[iso_utc(moment), kept[moment]] for moment in sorted(kept)]
