"""Tell the owner when a family period needs the manager to block its dates.

The page marks every period with where it stands, but only for whoever opens
it. A period still for sale that nobody notices is how guests end up booking
the family's dates, so the nightly job also pushes: it keeps one issue of this
repository, assigned to the owner, open for as long as something is waiting.
GitHub writes to the assignee, which is the whole mechanism.

The issue is written by the workflow's own token, not the owner's: nobody is
notified of what they do themselves. It is public like the repository, so its
text is generic: no dates, no names, no house. The detail is in the page.

What starts a message:

* a period enters "to be blocked" or "guests are booked on it" (a period just
  written on nights still for sale, the manager reopening dates, a booking
  arriving on top of it);
* on Mondays, a reminder while such periods remain that begin within
  ``HORIZON_DAYS``.

When nothing is waiting any more the job closes the issue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

from .family import CONFLICT, PENDING, FamilyEntry, entry_status
from .familylog import FamilyLogError, GitHubIssues

ISSUE_TITLE = "Date da far bloccare al gestore"
HORIZON_DAYS = 90
NEEDS_ACTION = (PENDING, CONFLICT)

NOTHING, OPEN, NEW, REMIND, CLOSE = "nothing", "open", "new", "remind", "close"

_WHERE = "Apri il calendario, sblocca l'area proprietà e guarda l'elenco «Da far bloccare al gestore»."
ISSUE_BODY = (
    "Nel calendario ci sono periodi segnati dalla famiglia su date che il gestore non ha ancora "
    "bloccato, oppure su cui nel frattempo sono arrivate prenotazioni di ospiti.\n\n"
    f"{_WHERE}\n\n"
    "Questa segnalazione è scritta dal job notturno e si chiude da sola quando non resta nulla "
    "in sospeso. Non riporta date né nomi perché il repository è pubblico."
)
NEW_TEXT = f"Ci sono nuovi periodi da far bloccare, o periodi su cui sono arrivati ospiti. {_WHERE}"
REMINDER_TEXT = (
    f"Promemoria del lunedì: restano periodi non bloccati che iniziano entro {HORIZON_DAYS} giorni. {_WHERE}"
)
CLOSING_TEXT = "Non resta nulla in sospeso: chiudo la segnalazione."


def needing_action(
    entries: Iterable[FamilyEntry], houses: Mapping[str, Any], today: date
) -> dict[str, str]:
    """``{entry id: "pending" | "conflict"}`` for the periods not yet over that need someone to act.

    ``houses`` holds only the houses whose calendar can be trusted for this:
    a period of any other house says nothing either way.
    """
    waiting: dict[str, str] = {}
    for entry in entries:
        if entry.end <= today or entry.prop not in houses:
            continue
        state = entry_status(entry, houses[entry.prop])
        if state in NEEDS_ACTION:
            waiting[entry.id] = state
    return waiting


def decide(
    waiting: Mapping[str, str],
    before: Mapping[str, str] | None,
    *,
    soon: bool,
    monday: bool,
    issue_open: bool,
    complete: bool = True,
) -> str:
    """What to do with the issue tonight.

    ``before`` is what was waiting according to the page currently online, or
    ``None`` when that page could not be read: then nothing counts as new, so
    that a page that cannot be read does not turn into a message every night.
    ``soon`` says whether something waiting begins within the horizon.
    ``complete`` is false when a house could not be judged tonight: an empty
    list then proves nothing, and the issue is left as it is.
    """
    if not waiting:
        return CLOSE if issue_open and complete else NOTHING
    changed = before is not None and any(before.get(entry) != state for entry, state in waiting.items())
    if changed:
        return NEW if issue_open else OPEN
    if monday and soon:
        return REMIND if issue_open else OPEN
    return NOTHING


@dataclass
class AlertOutcome:
    action: str
    waiting: int  # how many periods need someone to act (a count is all a public log gets)


def reconcile(
    issues: GitHubIssues,
    *,
    owner: str,
    entries: Iterable[FamilyEntry],
    houses: Mapping[str, Any],
    previous_entries: Iterable[FamilyEntry] | None,
    previous_houses: Mapping[str, Any] | None,
    today: date,
    complete: bool = True,
) -> AlertOutcome:
    """Bring the issue in line with what is waiting. Raises ``FamilyLogError`` if GitHub refuses.

    ``previous_*`` describe the page currently online (``None``: it could not be
    read); ``complete`` says whether ``houses`` holds every house of the calendar.
    """
    entries = list(entries)
    waiting = needing_action(entries, houses, today)
    before = (
        None
        if previous_entries is None or previous_houses is None
        else needing_action(previous_entries, previous_houses, today)
    )
    horizon = today + timedelta(days=HORIZON_DAYS)
    soon = any(entry.id in waiting and entry.start <= horizon for entry in entries)

    current = issues.issue_titled(ISSUE_TITLE, state="open", what="ricerca della segnalazione")
    action = decide(
        waiting, before, soon=soon, monday=today.weekday() == 0, issue_open=current is not None, complete=complete
    )
    mention = f"@{owner} " if owner else ""
    if action == OPEN:
        _open(issues, owner, mention)
    elif action in (NEW, REMIND):
        text = NEW_TEXT if action == NEW else REMINDER_TEXT
        issues.comment(int(current["number"]), mention + text, what="avviso")
    elif action == CLOSE:
        number = int(current["number"])
        issues.comment(number, CLOSING_TEXT, what="chiusura della segnalazione")
        issues.close_issue(number, what="chiusura della segnalazione")
    return AlertOutcome(action=action, waiting=len(waiting))


def _open(issues: GitHubIssues, owner: str, mention: str) -> None:
    body = mention + ISSUE_BODY
    if owner:
        try:
            issues.create_issue(ISSUE_TITLE, body, what="apertura della segnalazione", assignees=[owner])
            return
        except FamilyLogError as exc:
            # An organisation cannot be assigned an issue: the mention in the text still reaches it.
            if "HTTP 422" not in str(exc):
                raise
    issues.create_issue(ISSUE_TITLE, body, what="apertura della segnalazione")
