"""Configuration: what is public lives in ``properties.toml``, the rest in secrets.

The repository is public, so the file only names the houses. Portal addresses,
usernames, passwords and the two passphrases are read from the environment
(GitHub Actions secrets in production).
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

DEFAULT_CONFIG = "properties.toml"
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,30}$")
_CLOCK_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

DEFAULT_CHECK_IN = "15:00"
DEFAULT_CHECK_OUT = "11:00"

# Secrets the program can do without. FAMILY_LOG_TOKEN turns on the family's own
# entries (without it the page is read-only); OLD_FAMILY_PASSPHRASE is set for one
# run after the family passphrase has been changed, to carry those entries over.
OPTIONAL_SECRETS = ("FAMILY_LOG_TOKEN", "OLD_FAMILY_PASSPHRASE")


class ConfigError(ValueError):
    """The configuration file or the environment is incomplete."""


@dataclass(frozen=True)
class PropertyConfig:
    id: str
    label: str
    env: str  # prefix of the secrets: <env>_KB_URL, <env>_KB_USER, <env>_KB_PASS
    # House rules: guests arrive from check_in and leave by check_out (HH:MM, 24h).
    check_in: str = DEFAULT_CHECK_IN
    check_out: str = DEFAULT_CHECK_OUT

    def secret_names(self) -> tuple[str, str, str]:
        return (f"{self.env}_KB_URL", f"{self.env}_KB_USER", f"{self.env}_KB_PASS")

    @property
    def unit_variable(self) -> str:
        return f"{self.env}_KB_UNIT"


@dataclass(frozen=True)
class PortalCredentials:
    base_url: str
    username: str
    password: str
    unit: str | None = None  # apartment name, only needed when an account holds several


@dataclass(frozen=True)
class Config:
    title: str = "Calendario case"
    timezone: str = "Europe/Rome"
    site_id: str = "casecal"
    note: str | None = None
    years_ahead: int = 1
    properties: tuple[PropertyConfig, ...] = field(default_factory=tuple)


def _house_hours(path, where: str, entry: Mapping, check_in: str, check_out: str) -> tuple[str, str]:
    """Check-in and check-out time of a house, falling back to the given defaults."""
    check_in = str(entry.get("check_in", check_in))
    check_out = str(entry.get("check_out", check_out))
    for name, value in (("check_in", check_in), ("check_out", check_out)):
        if not _CLOCK_RE.match(value):
            raise ConfigError(f"{path}: {where}{name} deve essere un orario HH:MM, es. \"15:00\"")
    if not check_out < check_in:
        # Same-day turnovers rely on guests leaving before the next ones arrive.
        raise ConfigError(f"{path}: {where}check_out deve precedere check_in nella giornata")
    return check_in, check_out


def load_config(path: str | os.PathLike[str] = DEFAULT_CONFIG) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"file di configurazione non trovato: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML non valido ({exc})") from exc

    check_in, check_out = _house_hours(path, "", raw, DEFAULT_CHECK_IN, DEFAULT_CHECK_OUT)
    properties: list[PropertyConfig] = []
    seen: set[str] = set()
    for entry in raw.get("property", []):
        try:
            identity = dict(id=entry["id"], label=entry["label"], env=entry["env"])
        except KeyError as exc:
            raise ConfigError(f"{path}: a [[property]] manca il campo {exc.args[0]}") from exc
        hours = _house_hours(path, f"[[property]] {identity['id']}: ", entry, check_in, check_out)
        prop = PropertyConfig(**identity, check_in=hours[0], check_out=hours[1])
        if not _ID_RE.match(prop.id):
            raise ConfigError(f"{path}: id non valido {prop.id!r} (minuscole, cifre, trattini)")
        if not _ENV_RE.match(prop.env):
            raise ConfigError(f"{path}: env non valido {prop.env!r} (MAIUSCOLE, cifre, _)")
        if prop.id in seen:
            raise ConfigError(f"{path}: id duplicato {prop.id!r}")
        seen.add(prop.id)
        properties.append(prop)
    if not properties:
        raise ConfigError(f"{path}: nessuna [[property]] definita")

    years_ahead = int(raw.get("years_ahead", 1))
    if not 0 <= years_ahead <= 3:
        raise ConfigError(f"{path}: years_ahead deve essere tra 0 e 3")

    return Config(
        title=str(raw.get("title", "Calendario case")),
        timezone=str(raw.get("timezone", "Europe/Rome")),
        site_id=str(raw.get("site_id") or os.environ.get("GITHUB_REPOSITORY") or "casecal"),
        note=raw.get("note") or None,
        years_ahead=years_ahead,
        properties=tuple(properties),
    )


def missing_secrets(config: Config, environ: Mapping[str, str]) -> list[str]:
    """Names (never values) of the secrets that are not set."""
    names = ["FAMILY_PASSPHRASE", "OWNER_PASSPHRASE"]
    for prop in config.properties:
        names.extend(prop.secret_names())
    return [name for name in names if not environ.get(name, "").strip()]


def credentials_for(prop: PropertyConfig, environ: Mapping[str, str]) -> PortalCredentials:
    url_name, user_name, pass_name = prop.secret_names()
    base_url = environ.get(url_name, "").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise ConfigError(f"{url_name} deve essere un indirizzo https://")
    return PortalCredentials(
        base_url=base_url,
        username=environ.get(user_name, "").strip(),
        password=environ.get(pass_name, ""),
        unit=environ.get(prop.unit_variable, "").strip() or None,
    )
