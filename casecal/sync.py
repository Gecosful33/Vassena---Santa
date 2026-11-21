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

from . import r2
from .alerts import needing_action, reconcile
from .config import Config, PortalCredentials, PropertyConfig
from .costs import CostSheet, CostsError, parse_workbook, sheets_from_payload
from .crypto import EnvelopeError, build_envelope, derive_key, open_envelope, site_salt
from .errors import PortalError, SyncAborted
from .family import FamilyEntry, FamilyError, ordered
from .familylog import (
    DEFAULT_API,
    FamilyLogError,
    FamilyState,
    GitHubIssues,
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
from .photos import MAX_SOURCE_BYTES, Photo, PhotoError, photos_from_payload, prepare
from .site import SiteError, fetch_published_envelope, render_page, write_site

Fetcher = Callable[[PropertyConfig, PortalCredentials, Sequence[int], datetime], PropertySnapshot]


@dataclass
class PreviousPage:
    """What the page currently online holds: the job's only state between runs."""

    properties: dict[str, PropertySnapshot] = field(default_factory=dict)
    entries: list[FamilyEntry] = field(default_factory=list)
    log: dict[str, Any] | None = None
    costs: list[CostSheet] = field(default_factory=list)
    photos: dict[str, Photo] = field(default_factory=dict)


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
    site_url: str | None, *, passphrase: str, legacy_owner_passphrase: str | None = None
) -> PreviousPage:
    """What the page currently online holds, or an empty page if it cannot be read back.

    ``legacy_owner_passphrase`` opens the figures of a page published when they
    had a passphrase of their own; without it such a page still gives its calendar.
    """
    if not site_url:
        return PreviousPage()
    envelope = fetch_published_envelope(site_url)
    if envelope is None:
        return PreviousPage()
    try:
        family, owner = open_envelope(
            envelope, passphrase=passphrase, legacy_owner_passphrase=legacy_owner_passphrase
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
    return PreviousPage(
        properties=properties,
        entries=entries,
        log=log if isinstance(log, dict) else None,
        costs=sheets_from_payload((owner or {}).get("costs")),
        photos=photos_from_payload(family.get("photos")),
    )


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


@dataclass
class CostsState:
    """The houses' running costs for the page, and what went wrong reading them."""

    sheets: list[CostSheet]
    warnings: list[str]
    fresh: bool = False  # the owner's workbook was read in this run


def costs_state(
    config: Config,
    *,
    url: str | None,
    key_id: str | None,
    secret: str | None,
    load_previous: PreviousLoader,
    fetch: Callable[[str, str, str], bytes] | None = None,
) -> CostsState:
    """The cost sheets of the owner's workbook, fetched from its private bucket.

    Without the three settings there is no profit and loss, and nothing to
    report. With them, a workbook that cannot be fetched or trusted leaves the
    costs already published in place and flags the run: a slip in a spreadsheet
    must neither empty the page nor keep the calendar from being published.
    """
    given = [bool(url), bool(key_id), bool(secret)]
    if not any(given):
        return CostsState([], [])

    def kept(reason: str) -> CostsState:
        return CostsState(
            list(load_previous().costs),
            [f"spese non lette ({reason}): restano quelle già pubblicate"],
        )

    if not all(given):
        return kept("servono tutti e tre i secret COSTS_R2_URL, COSTS_R2_ACCESS_KEY_ID e COSTS_R2_SECRET_ACCESS_KEY")
    try:
        data = (fetch or r2.fetch)(url, key_id, secret)
        sheets, warnings = parse_workbook(data, [(prop.id, prop.label) for prop in config.properties])
    except (r2.StorageError, CostsError) as exc:
        return kept(str(exc))  # written to be public: no address, no key, nothing of the content
    except Exception as exc:  # noqa: BLE001 - the calendar comes first
        return kept(describe_error(exc))
    return CostsState(sheets, warnings, fresh=True)


@dataclass
class PhotosState:
    """The houses' photos for the page, and what went wrong reading them."""

    photos: dict[str, Photo]
    warnings: list[str]
    fresh: list[str] = field(default_factory=list)  # the houses whose photo was read in this run


def photos_state(
    config: Config,
    *,
    url: str | None,
    key_id: str | None,
    secret: str | None,
    load_previous: PreviousLoader,
    fetch: Callable[..., bytes] | None = None,
) -> PhotosState:
    """A photo of each house, from the bucket that holds the workbook of costs.

    The photo of a house is the file ``<house id>.jpg`` kept beside the
    workbook: ``url`` is the workbook's address, and tells where to look. A
    house without such a file simply has no photo. One that cannot be fetched,
    or is not a photo, keeps the picture already published and flags the run;
    like the costs, nothing about the photos can stop the calendar.
    """
    given = [bool(url), bool(key_id), bool(secret)]
    if not any(given):
        return PhotosState({}, [])
    if not all(given):
        # Said once already, where the costs are read: here it only means "leave things as they are".
        return PhotosState(dict(load_previous().photos), [])

    photos: dict[str, Photo] = {}
    fresh: list[str] = []
    failed: dict[str, list[str]] = {}  # reason -> houses
    for prop in config.properties:
        try:
            raw = (fetch or r2.fetch)(r2.sibling(url, f"{prop.id}.jpg"), key_id, secret, max_bytes=MAX_SOURCE_BYTES)
            photos[prop.id] = prepare(raw)
            fresh.append(prop.id)
        except r2.NotFound:
            continue  # no photo of this house: nothing to show, nothing to report
        except (r2.StorageError, PhotoError) as exc:
            failed.setdefault(str(exc), []).append(prop.id)  # written to be public
        except Exception as exc:  # noqa: BLE001 - the calendar comes first
            failed.setdefault(describe_error(exc), []).append(prop.id)

    warnings: list[str] = []
    if failed:
        published = load_previous().photos
        for reason, houses in failed.items():
            for house in houses:
                if house in published:
                    photos[house] = published[house]
            warnings.append(f"foto non lette ({reason}): {', '.join(houses)}; restano quelle già pubblicate")
    return PhotosState(photos, warnings, fresh)


def alert_owner(
    config: Config,
    snapshot: Snapshot,
    report: SyncReport,
    family: FamilyState,
    *,
    token: str | None,
    repository: str | None,
    owner: str | None,
    load_previous: PreviousLoader,
    now: datetime,
    api: str = DEFAULT_API,
) -> tuple[str | None, str | None]:
    """Keep the owner's alert in line with tonight's calendar (see ``alerts.py``).

    Returns what was done with the alert and, separately, what went wrong.
    Nothing is done unless the family's log was read in this very run: entries
    carried over from the page online are too old a basis to write to someone.
    """
    if not token or not repository or not family.fresh:
        return None, None
    today = now.astimezone(ZoneInfo(config.timezone)).date()
    # Only the houses read tonight without a hitch: a period of any other house proves nothing.
    houses = {
        prop.id: prop
        for prop in snapshot.properties
        if prop.id in report.fresh and prop.id not in report.warnings
    }
    previous_entries = previous_houses = None
    if needing_action(family.entries, houses, today):
        previous = load_previous()
        if previous.properties:  # an empty page is one that could not be read back
            previous_entries, previous_houses = previous.entries, previous.properties
    try:
        outcome = reconcile(
            GitHubIssues(token, repository, api=api),
            owner=owner or "",
            entries=family.entries,
            houses=houses,
            previous_entries=previous_entries,
            previous_houses=previous_houses,
            today=today,
            complete=len(houses) == len(config.properties),
        )
    except FamilyLogError as exc:
        return None, f"avviso al proprietario non aggiornato ({exc})"
    return outcome.action, None


def publish(
    snapshot: Snapshot,
    *,
    passphrase: str,
    site_id: str,
    out_dir: str | Path,
) -> Path:
    family_payload = snapshot.family_payload()
    owner_payload = snapshot.owner_payload()
    envelope = build_envelope(
        passphrase=passphrase,
        salt=site_salt(site_id),
        family_payload=family_payload,
        owner_payload=owner_payload,
    )
    # Never publish a page that its own passphrase cannot open.
    family, owner = open_envelope(envelope, passphrase=passphrase)
    if family != family_payload or owner != owner_payload:
        raise SiteError("envelope self-check failed")
    return write_site(out_dir, render_page(envelope, title=snapshot.title))
