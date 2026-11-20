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


class ConfigError(ValueError):
    """The configuration file or the environment is incomplete."""


@dataclass(frozen=True)
class PropertyConfig:
    id: str
    label: str
    env: str  # prefix of the secrets: <env>_KB_URL, <env>_KB_USER, <env>_KB_PASS

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


def load_config(path: str | os.PathLike[str] = DEFAULT_CONFIG) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"file di configurazione non trovato: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML non valido ({exc})") from exc

    properties: list[PropertyConfig] = []
    seen: set[str] = set()
    for entry in raw.get("property", []):
        try:
            prop = PropertyConfig(id=entry["id"], label=entry["label"], env=entry["env"])
        except KeyError as exc:
            raise ConfigError(f"{path}: a [[property]] manca il campo {exc.args[0]}") from exc
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
