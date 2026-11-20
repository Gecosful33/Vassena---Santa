"""Nightly orchestration: read every portal, fall back to the last good data, publish.

The two houses are run by different managers, so their portals fail
independently (a changed password, a redesign, an outage). One failure must not
freeze the other house's calendar: the house that could not be read keeps the
data it had in the page currently online, with its original timestamp, and the
page warns about it.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .config import Config, PortalCredentials, PropertyConfig
from .crypto import EnvelopeError, build_envelope, derive_key, open_envelope, site_salt
from .errors import PortalError, SyncAborted
from .family import FamilyEntry, FamilyError, ordered
from .familylog import (
    DEFAULT_API,
    FamilyLogError,
    FamilyState,
    GitHubLog,
    read_family_state,
    token_problem,
)
from .model import (
    DataError,
    MissingProperty,
    PropertySnapshot,
    Snapshot,
    properties_from_payloads,
)
from .site import SiteError, fetch_published_envelope, render_page, write_site

Fetcher = Callable[[PropertyConfig, PortalCredentials, Sequence[int], datetime], PropertySnapshot]


@dataclass
class PreviousPage:
    """What the page currently online holds: the job's only state between runs."""

    properties: dict[str, PropertySnapshot] = field(default_factory=dict)
    entries: list[FamilyEntry] = field(default_factory=list)
    log: dict[str, Any] | None = None


PreviousLoader = Callable[[], PreviousPage]


@dataclass
class SyncReport:
    fresh: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, list[str]] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        """Something needs a human: the run publishes what it has, then fails."""
        return bool(self.reused or self.missing or self.warnings)


def describe_error(exc: BaseException) -> str:
    """One line that is safe for a public log.

    Only ``PortalError`` messages are written to be public. Any other exception
    might quote scraped content, so it is reduced to its type and the line of
    this package where it surfaced.
    """
    if isinstance(exc, PortalError):
        return f"{type(exc).__name__}: {exc}"
    where = ""
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        path = Path(frame.filename)
        if path.parent.name == "casecal":
            where = f" ({path.name}:{frame.lineno})"
            break
    return f"{type(exc).__name__}{where}"


def target_years(config: Config, now: datetime) -> list[int]:
    this_year = now.astimezone(ZoneInfo(config.timezone)).year
    return [this_year + offset for offset in range(config.years_ahead + 1)]


def collect(
    config: Config,
    credentials: Mapping[str, PortalCredentials],
    *,
    fetch: Fetcher,
    load_previous: PreviousLoader,
    now: datetime,
) -> tuple[Snapshot, SyncReport]:
    report = SyncReport()
    years = target_years(config, now)
    fetched: dict[str, PropertySnapshot] = {}
    for prop in config.properties:
        try:
            snapshot = fetch(prop, credentials[prop.id], years, now)
        except Exception as exc:  # noqa: BLE001 - one portal must not take the other down
            report.errors[prop.id] = describe_error(exc)
            continue
        fetched[prop.id] = snapshot
        report.fresh.append(prop.id)
        if snapshot.warnings:
            report.warnings[prop.id] = list(snapshot.warnings)

    if not fetched:
        raise SyncAborted("nessun portale letto: la pagina online resta invariata")

    previous = load_previous().properties if report.errors else {}
    properties: list[PropertySnapshot | MissingProperty] = []
    for prop in config.properties:
        if prop.id in fetched:
            properties.append(fetched[prop.id])
        elif prop.id in previous:
            stale = previous[prop.id]
            # Name and house rules are configuration: the current ones win over the stored copy.
            stale.label = prop.label
            stale.check_in_time, stale.check_out_time = prop.check_in, prop.check_out
            properties.append(stale)
            report.reused.append(prop.id)
        else:
            properties.append(MissingProperty(id=prop.id, label=prop.label))
            report.missing.append(prop.id)

    snapshot = Snapshot(
        generated_at=now,
        properties=properties,
        title=config.title,
        timezone=config.timezone,
        note=config.note,
    )
    return snapshot, report


def previous_from_site(
    site_url: str | None, *, family_passphrase: str, owner_passphrase: str
) -> PreviousPage:
    """What the page currently online holds, or an empty page if it cannot be read back."""
    if not site_url:
        return PreviousPage()
    envelope = fetch_published_envelope(site_url)
    if envelope is None:
        return PreviousPage()
    try:
        family, owner = open_envelope(
            envelope, family_passphrase=family_passphrase, owner_passphrase=owner_passphrase
        )
    except EnvelopeError:
        return PreviousPage()
    if family is None or family.get("demo"):
        return PreviousPage()
    try:
        properties = properties_from_payloads(family, owner)
    except (DataError, KeyError, TypeError, ValueError):
        properties = {}
    entries: list[FamilyEntry] = []
    for raw in family.get("entries") or []:
        try:
            entries.append(FamilyEntry.from_dict(raw))
        except FamilyError:
            continue
    log = family.get("log")
    return PreviousPage(properties=properties, entries=entries, log=log if isinstance(log, dict) else None)


def family_state(
    config: Config,
    *,
    token: str | None,
    repository: str | None,
    family_passphrase: str,
    old_family_passphrase: str | None,
    load_previous: PreviousLoader,
    api: str = DEFAULT_API,
) -> FamilyState:
    """The family's entries and the settings that let the page edit them.

    Without a token the feature is simply off: the page stays read-only.
    """
    if not token:
        return FamilyState(entries=[], log=None, warnings=[])

    def keep_published() -> FamilyState:
        previous = load_previous()
        return FamilyState(entries=ordered(previous.entries), log=previous.log, warnings=[])

    unsafe = token_problem(token)
    if unsafe:
        # The entries stay visible, but no token is handed to the page: not this one,
        # and not the one the page online may still carry.
        kept = keep_published()
        return FamilyState(entries=kept.entries, log=None, warnings=[unsafe])

    try:
        log = GitHubLog(token, repository or "", api=api)
    except FamilyLogError as exc:
        kept = keep_published()
        kept.warnings.append(f"registro di famiglia non configurato ({exc})")
        return kept
    salt = site_salt(config.site_id)
    return read_family_state(
        log,
        key=derive_key(family_passphrase, salt),
        old_key=derive_key(old_family_passphrase, salt) if old_family_passphrase else None,
        token=token,
        known_houses={prop.id for prop in config.properties},
        fallback=keep_published,
    )


def publish(
    snapshot: Snapshot,
    *,
    family_passphrase: str,
    owner_passphrase: str,
    site_id: str,
    out_dir: str | Path,
) -> Path:
    family_payload = snapshot.family_payload()
    owner_payload = snapshot.owner_payload()
    envelope = build_envelope(
        family_passphrase=family_passphrase,
        owner_passphrase=owner_passphrase,
        salt=site_salt(site_id),
        family_payload=family_payload,
        owner_payload=owner_payload,
    )
    # Never publish a page that its own passphrases cannot open.
    family, owner = open_envelope(
        envelope, family_passphrase=family_passphrase, owner_passphrase=owner_passphrase
    )
    if family != family_payload or owner is None or owner.get("properties") != owner_payload["properties"]:
        raise SiteError("envelope self-check failed")
    return write_site(out_dir, render_page(envelope, title=snapshot.title))
