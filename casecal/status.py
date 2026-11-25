"""The control panel at the top of each house: WiFi, heating and cooling, the lake.

None of this comes from the managers' portals. Heating and cooling are read
from the makers' own clouds, the ones behind the apps the family already uses
(see ``devices.py``); whether the house's WiFi is up is told by those same
units, which reach their cloud through it; the lake's level is public (see
``lake.py``).

What a house has is said in ``properties.toml``; the accounts are secrets. A
source that cannot be read is shown as "not read", never as off: a panel that
guessed would be worse than none. Nothing here can stop the calendar.

The states travel in the encrypted part of the page, with the calendar. The
public log only ever says which source was read and what kind of error another
gave: never a name, a temperature or a state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from . import lake as lakes
from .config import ClimateConfig, Config, LakeConfig
from .devices import SOURCES, Device, DeviceError
from .model import iso_utc, parse_utc

ONLINE, OFFLINE, UNKNOWN = "online", "offline", "unknown"
ON, OFF = "on", "off"

DeviceReader = Callable[[str, Mapping[str, str]], Sequence[Device]]
LakeReader = Callable[[str], lakes.LakeReading]


@dataclass
class StatusReport:
    """What the public log may say of a round of readings."""

    read: list[str] = field(default_factory=list)  # sources read
    off: list[str] = field(default_factory=list)  # sources a house names, whose secrets are not set
    warnings: list[str] = field(default_factory=list)


def configured(config: Config) -> bool:
    """Whether any house has something for the panel to show."""
    return any(prop.climate or prop.lake for prop in config.properties)


def _read_devices(source: str, environ: Mapping[str, str]) -> Sequence[Device]:
    return SOURCES[source].read(environ)


def _matching(devices: Sequence[Device], only: str | None) -> list[Device]:
    if not only:
        return list(devices)
    wanted = only.casefold()
    return [device for device in devices if wanted in device.name.casefold()]


def _mean(values: Sequence[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return round(sum(known) / len(known), 1) if known else None


def climate_entry(entry: ClimateConfig, devices: Sequence[Device] | None) -> dict[str, Any]:
    """One row of the panel: the units of one app in one house, taken together.

    ``devices`` is ``None`` when the app's cloud could not be read.
    """
    row: dict[str, Any] = {"label": entry.label, "app": SOURCES[entry.source].label, "state": UNKNOWN}
    if devices is None:
        return row
    mine = _matching(devices, entry.only)
    if not mine:
        return row  # an account with no unit of this house: nothing can be said
    reachable = [device for device in mine if device.online is not False]
    if not reachable:
        row["state"] = OFFLINE
        return row
    known = [device for device in reachable if device.on is not None]
    running = [device for device in known if device.on]
    if not known:
        return row
    row["state"] = ON if running else OFF
    row["units"], row["running"] = len(known), len(running)
    modes = {device.mode for device in running if device.mode}
    if len(modes) == 1:
        row["mode"] = modes.pop()
    row["room"] = _mean([device.room for device in known])
    if running:
        row["target"] = _mean([device.target for device in running])
    return row


def wifi_entry(label: str | None, readings: Sequence[Sequence[Device] | None]) -> dict[str, Any]:
    """Whether the house's WiFi is up, as far as its units can tell.

    A unit that its cloud sees is proof that the WiFi works. Units that are all
    out of reach are the sign that it does not (or that the power is out, which
    calls for the same visit). No unit read: nothing is known.
    """
    seen = [device for devices in readings if devices is not None for device in devices if device.online is not None]
    state = UNKNOWN
    if any(device.online for device in seen):
        state = ONLINE
    elif seen:
        state = OFFLINE
    return {"label": label, "state": state}


def lake_entry(
    setting: LakeConfig,
    reading: lakes.LakeReading | None,
    previous: Mapping[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """The lake as the panel shows it: the level, where it stands, and the last days' readings."""
    previous = previous if isinstance(previous, Mapping) else {}
    row: dict[str, Any] = {
        "name": setting.name,
        "attention_cm": setting.attention_cm,
        "alarm_cm": setting.alarm_cm,
        "history": lakes.extend_history(previous.get("history"), reading, now),
        "source": lakes.SOURCE,
    }
    before = previous.get("stage") if previous.get("stage") in (lakes.NORMAL, lakes.ATTENTION, lakes.ALARM) else None
    if reading is None or now - reading.read_at > lakes.MAX_AGE:
        # The level is not known now. Where it stood is kept, for the alert to start from.
        if before:
            row["stage_before"] = before
        return row
    row.update(
        level_cm=reading.level_cm,
        read_at=iso_utc(reading.read_at),
        stage=lakes.stage(reading.level_cm, setting.attention_cm, setting.alarm_cm, before or previous.get("stage_before")),
        trend=reading.trend,
        inflow=reading.inflow,
        outflow=reading.outflow,
        fill=reading.fill,
    )
    return row


def collect_status(
    config: Config,
    environ: Mapping[str, str],
    *,
    previous: Mapping[str, Any] | None,
    now: datetime,
    read_devices: DeviceReader = _read_devices,
    read_lake: LakeReader = lakes.fetch_reading,
) -> tuple[dict[str, Any] | None, StatusReport]:
    """Read every source the houses name and build the panel's part of the page.

    ``previous`` is that part of the page currently online (the lake's history
    is carried on from it). Returns ``None`` when no house has a panel.
    """
    report = StatusReport()
    if not configured(config):
        return None, report
    before = previous.get("houses") if isinstance(previous, Mapping) else None
    before = before if isinstance(before, Mapping) else {}

    # Each app's cloud is asked once, whichever houses its units are in.
    by_source: dict[str, Sequence[Device] | None] = {}
    for source in sorted({entry.source for prop in config.properties for entry in prop.climate}):
        missing = [name for name in SOURCES[source].secrets if not environ.get(name, "").strip()]
        if missing:
            by_source[source] = None
            report.off.append(source)
            continue
        try:
            by_source[source] = list(read_devices(source, environ))
            report.read.append(source)
        except DeviceError as exc:
            by_source[source] = None
            report.warnings.append(f"{source}: stato non letto ({exc})")  # written to be public
        except Exception as exc:  # noqa: BLE001 - the calendar comes first
            by_source[source] = None
            report.warnings.append(f"{source}: stato non letto ({type(exc).__name__})")

    houses: dict[str, Any] = {}
    for prop in config.properties:
        if not (prop.climate or prop.lake):
            continue
        house: dict[str, Any] = {}
        if prop.climate:
            readings = [
                None if by_source[entry.source] is None else _matching(by_source[entry.source], entry.only)
                for entry in prop.climate
            ]
            house["wifi"] = wifi_entry(prop.wifi, readings)
            house["climate"] = [climate_entry(entry, by_source[entry.source]) for entry in prop.climate]
        if prop.lake:
            reading = None
            try:
                reading = read_lake(prop.lake.name)
                report.read.append("lago")
            except lakes.LakeError as exc:
                report.warnings.append(f"lago: livello non letto ({exc})")  # written to be public
            except Exception as exc:  # noqa: BLE001 - the calendar comes first
                report.warnings.append(f"lago: livello non letto ({type(exc).__name__})")
            earlier = before.get(prop.id) if isinstance(before.get(prop.id), Mapping) else {}
            house["lake"] = lake_entry(prop.lake, reading, earlier.get("lake"), now)
        houses[prop.id] = house
    return {"checked_at": iso_utc(now), "houses": houses}, report


def lake_stages(status: Mapping[str, Any] | None) -> dict[str, str]:
    """``{house id: stage}`` of the lakes whose level is known in this part of a page."""
    stages: dict[str, str] = {}
    houses = status.get("houses") if isinstance(status, Mapping) else None
    for house, held in (houses.items() if isinstance(houses, Mapping) else []):
        lake = held.get("lake") if isinstance(held, Mapping) else None
        if isinstance(lake, Mapping) and lake.get("stage") in (lakes.NORMAL, lakes.ATTENTION, lakes.ALARM):
            stages[str(house)] = lake["stage"]
    return stages


def checked_at(status: Mapping[str, Any] | None) -> datetime | None:
    try:
        return parse_utc(status["checked_at"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return None
