"""Where the family's entries are kept: the comments of one issue of this repository.

Each comment is one encrypted event (see ``family.py``). The repository is
public, so anyone can read the comments, but they are ciphertext; the issue is
locked, so only the owner's token can add to it. Nothing here is committed to
the repository and no other service is involved.

The page reads and writes the same comments straight from the browser with a
token limited to the issues of this one repository. That token is delivered to
family members inside the encrypted page, which is why its permissions must
stay that narrow: it must not be able to touch code, workflows or secrets.

No API version is pinned, here or in the page: the page cannot send the version
header (it is not among those GitHub accepts from browsers), a pinned version
is eventually retired, and only the oldest fields of the oldest endpoints are
read. Anything unexpected in an answer is reported as a failed read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

import requests

from .family import (
    FamilyEntry,
    decode_event,
    encode_event,
    fold,
    is_event,
    ordered,
    snapshot_event,
)

ISSUE_TITLE = "Registro di famiglia (dati cifrati: non modificare)"
ISSUE_BODY = (
    "Questa issue è l'archivio delle voci inserite dalla famiglia nel calendario.\n\n"
    "Ogni commento è una modifica cifrata: senza la password del calendario non è leggibile. "
    "Non modificare e non cancellare i commenti, e non chiudere la issue: "
    "il calendario li rilegge a ogni apertura."
)
DEFAULT_API = "https://api.github.com"
PAGE_SIZE = 100
MAX_PAGES = 200
TIMEOUT_SECONDS = 20
# A fine-grained token is the only kind that can be limited to the issues of one
# repository. A classic one ("ghp_...") reaches every repository of its owner.
FINE_GRAINED_PREFIX = "github_pat_"
# The page asks GitHub only for comments touched after this long before the last
# one the job read; the exact cut is then made on comment ids.
SINCE_MARGIN = timedelta(hours=1)

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class FamilyLogError(RuntimeError):
    """The log could not be read or written. The message is safe for a public log."""


def token_problem(token: str) -> str | None:
    """Why this token must not be handed to the page, or ``None`` if it may be.

    The page delivers the token to everyone who knows the family passphrase, so
    it has to be one that can do nothing beyond the issues of this repository.
    """
    if not token.startswith(FINE_GRAINED_PREFIX):
        return (
            "FAMILY_LOG_TOKEN non è un token fine-grained (deve iniziare con "
            f"{FINE_GRAINED_PREFIX}): un token classico arriva a tutti i repository dell'account"
        )
    return None


@dataclass(frozen=True)
class LogComment:
    id: int
    body: str
    created: str = ""  # GitHub's creation time, e.g. 2026-11-20T12:30:00Z


def _comment(raw: Any, what: str) -> LogComment:
    try:
        return LogComment(int(raw["id"]), str(raw.get("body") or ""), str(raw.get("created_at") or ""))
    except (AttributeError, KeyError, TypeError, ValueError):
        raise FamilyLogError(f"{what}: risposta inattesa") from None


def _rate_limited(response: Any) -> bool:
    """A 403 that means "slow down", not "not allowed": GitHub uses the same status for both."""
    if getattr(response, "headers", {}).get("x-ratelimit-remaining") == "0":
        return True
    try:
        message = response.json().get("message")
    except (AttributeError, ValueError):
        return False
    return isinstance(message, str) and "rate limit" in message.lower()


def _is_pull_request(issue: Mapping[str, Any]) -> bool:
    """The issues endpoint lists pull requests too; those carry links under "pull_request"."""
    links = issue.get("pull_request")
    return isinstance(links, Mapping) and any(links.values())


def since_for(comment: LogComment | None) -> str | None:
    """The ``since`` filter that still returns everything written after ``comment``."""
    if comment is None:
        return None
    try:
        created = datetime.strptime(comment.created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (created - SINCE_MARGIN).strftime("%Y-%m-%dT%H:%M:%SZ")


class GitHubIssues:
    """The issues of one repository, as far as this program needs them."""

    def __init__(
        self,
        token: str,
        repo: str,
        *,
        api: str = DEFAULT_API,
        session: requests.Session | None = None,
    ) -> None:
        if not _REPO_RE.match(repo or ""):
            raise FamilyLogError("repository non valido")
        self.repo = repo
        self.api = api.rstrip("/")
        self.notes: list[str] = []  # problems that did not stop the read (safe to log)
        self._session = session or requests.Session()
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

    def _request(self, method: str, path: str, what: str, **kwargs) -> requests.Response:
        try:
            response = self._session.request(
                method,
                f"{self.api}/repos/{self.repo}{path}",
                headers=self._headers,
                timeout=TIMEOUT_SECONDS,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise FamilyLogError(f"{what}: GitHub non raggiungibile ({type(exc).__name__})") from None
        status = response.status_code
        if status == 401:
            raise FamilyLogError(f"{what}: token rifiutato (scaduto o revocato)")
        if status == 429 or (status == 403 and _rate_limited(response)):
            raise FamilyLogError(f"{what}: limite di richieste di GitHub raggiunto")
        if status in (403, 404):
            raise FamilyLogError(f"{what}: il token non ha il permesso Issues su questo repository")
        if status == 410:
            raise FamilyLogError(f"{what}: le Issues sono disattivate in questo repository")
        if status >= 400:
            raise FamilyLogError(f"{what}: HTTP {status}")
        return response

    def _json(self, method: str, path: str, what: str, **kwargs) -> Any:
        response = self._request(method, path, what, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise FamilyLogError(f"{what}: risposta inattesa") from None

    def _pages(self, path: str, what: str, params: Mapping[str, Any]) -> list[Any]:
        items: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            batch = self._json("GET", path, what, params={**params, "per_page": PAGE_SIZE, "page": page})
            if not isinstance(batch, list):
                raise FamilyLogError(f"{what}: risposta inattesa")
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                return items
        # Never a silent cut: the newest events are the ones that would go missing.
        raise FamilyLogError(f"{what}: troppi elementi (oltre {MAX_PAGES * PAGE_SIZE})")

    def issue_titled(self, title: str, *, state: str, what: str) -> dict[str, Any] | None:
        """The oldest issue (not pull request) with exactly this title, or ``None``."""
        issues = self._pages("/issues", what, {"state": state, "sort": "created", "direction": "asc"})
        try:
            for issue in issues:
                if issue.get("title") == title and not _is_pull_request(issue):
                    int(issue["number"])
                    return issue
        except (AttributeError, KeyError, TypeError, ValueError):
            raise FamilyLogError(f"{what}: risposta inattesa") from None
        return None

    def create_issue(self, title: str, body: str, *, what: str, assignees: list[str] | None = None) -> int:
        payload: dict[str, Any] = {"title": title, "body": body}
        if assignees:
            payload["assignees"] = assignees
        created = self._json("POST", "/issues", what, json=payload)
        try:
            return int(created["number"])
        except (KeyError, TypeError, ValueError):
            raise FamilyLogError(f"{what}: risposta inattesa") from None

    def comment(self, number: int, body: str, *, what: str) -> None:
        self._json("POST", f"/issues/{number}/comments", what, json={"body": body})

    def set_issue_body(self, number: int, body: str, *, what: str) -> None:
        self._json("PATCH", f"/issues/{number}", what, json={"body": body})

    def close_issue(self, number: int, *, what: str) -> None:
        self._json("PATCH", f"/issues/{number}", what, json={"state": "closed", "state_reason": "completed"})


class GitHubLog(GitHubIssues):
    """The family's log: one locked issue whose comments are the encrypted events."""

    def find_or_create_issue(self) -> int:
        """Number of the log issue; the oldest one with our title, created if there is none.

        The issue is kept locked: on a public repository anyone could otherwise
        add comments to it. They could not forge an entry, but they could bury
        the real ones.
        """
        issue = self.issue_titled(ISSUE_TITLE, state="all", what="ricerca del registro")
        if issue is not None:
            number = int(issue["number"])
            if issue.get("locked") is False:  # created but never locked, or unlocked by hand
                self._lock(number)
            return number
        number = self.create_issue(ISSUE_TITLE, ISSUE_BODY, what="creazione del registro")
        self._lock(number)
        return number

    def _lock(self, number: int) -> None:
        """Lock the log. A failure is reported but does not stop the entries from being read."""
        try:
            self._request("PUT", f"/issues/{number}/lock", "blocco del registro", json={"lock_reason": "resolved"})
        except FamilyLogError as exc:
            self.notes.append(f"{exc}: la issue del registro resta aperta ai commenti di chiunque finché non viene bloccata")

    def comments(self, issue: int) -> list[LogComment]:
        """Every comment of the log, oldest first."""
        what = "lettura del registro"
        comments = [_comment(raw, what) for raw in self._pages(f"/issues/{issue}/comments", what, {})]
        return sorted(comments, key=lambda comment: comment.id)

    def add_comment(self, issue: int, body: str) -> LogComment:
        what = "scrittura nel registro"
        created = _comment(self._json("POST", f"/issues/{issue}/comments", what, json={"body": body}), what)
        return LogComment(created.id, body, created.created)


@dataclass
class FamilyState:
    """What the page needs about the family layer."""

    entries: list[FamilyEntry]
    log: dict[str, Any] | None  # how the page reaches the log; None = editing is off
    warnings: list[str]
    fresh: bool = False  # the log itself was read in this run (not carried over from the page online)


def read_family_state(
    log: GitHubLog,
    *,
    key: bytes,
    old_key: bytes | None,
    token: str,
    known_houses: set[str],
    fallback: Callable[[], FamilyState],
) -> FamilyState:
    """Current entries from the log, and the settings the page needs to follow it.

    If GitHub cannot be reached or refuses the token, the entries already
    published are kept (``fallback``) and the run is flagged, so that a bad
    token never makes the family's notes vanish from the calendar.

    ``old_key`` covers a change of the family passphrase: events written under
    the previous key are still read, and the current state is written back once
    under the new key so that the old passphrase can then be forgotten.
    """
    try:
        issue = log.find_or_create_issue()
        comments = log.comments(issue)
    except FamilyLogError as exc:
        kept = fallback()
        kept.warnings.append(f"registro di famiglia non letto ({exc}): restano le voci già pubblicate")
        return kept

    events: list[dict[str, Any]] = []
    unreadable = 0
    migrated = False
    for comment in comments:
        event = decode_event(key, comment.body)
        if event is None and old_key is not None:
            event = decode_event(old_key, comment.body)
            migrated = migrated or event is not None
        if event is None:
            unreadable += 1 if is_event(comment.body) else 0
            continue
        if event.get("t") == "snap":
            unreadable = 0  # whatever came before a snapshot no longer matters
        events.append(event)

    entries = fold(events)
    last = comments[-1] if comments else None
    warnings: list[str] = list(log.notes)
    if migrated:
        try:
            last = log.add_comment(issue, encode_event(key, snapshot_event(entries.values())))
        except FamilyLogError as exc:
            warnings.append(f"registro di famiglia non aggiornato alla nuova password ({exc})")
    if unreadable:
        warnings.append(
            f"{unreadable} voci del registro di famiglia non si aprono con la password attuale: "
            "se l'hai cambiata, imposta per un'esecuzione il secret OLD_FAMILY_PASSPHRASE con quella precedente"
        )
    return FamilyState(
        entries=ordered(entry for entry in entries.values() if entry.prop in known_houses),
        # `cursor` is the last comment already folded into `entries`: the page reads only
        # newer ones. `since` narrows that request and always travels with its cursor.
        log={
            "kind": "github",
            "api": log.api,
            "repo": log.repo,
            "issue": issue,
            "token": token,
            "cursor": last.id if last else 0,
            "since": since_for(last),
        },
        warnings=warnings,
        fresh=True,
    )
