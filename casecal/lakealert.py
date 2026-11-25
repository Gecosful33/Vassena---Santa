"""Tell the owner when the lake is high enough to threaten the boat.

The panel shows the level, but only to whoever opens the page. So, as for the
dates to be blocked (see ``alerts.py``), the job keeps one issue of this
repository open for as long as the lake is above its first threshold, assigned
to the owner: GitHub writes to the assignee. It says so again when the lake
passes the second threshold, and closes the issue when the lake is back down.

The level of a lake is public, so the issue quotes it. It does not say whose
boat it is, nor where.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import lake as lakes
from .familylog import FamilyLogError, GitHubIssues

ISSUE_TITLE = "Lago alto: controllare la barca"
NOTHING, OPEN, WORSE, CLOSE = "nothing", "open", "worse", "close"
_RANK = {lakes.NORMAL: 0, lakes.ATTENTION: 1, lakes.ALARM: 2}
_WORDS = {lakes.ATTENTION: "soglia di attenzione superata", lakes.ALARM: "SOGLIA DI ALLARME SUPERATA"}
CLOSING_TEXT = "Il lago è tornato sotto la soglia di attenzione: chiudo la segnalazione."


def _lakes(status: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    houses = status.get("houses") if isinstance(status, Mapping) else None
    found = []
    for held in (houses.values() if isinstance(houses, Mapping) else []):
        lake = held.get("lake") if isinstance(held, Mapping) else None
        if isinstance(lake, Mapping):
            found.append(lake)
    return found


def _stage(lake: Mapping[str, Any]) -> str | None:
    """Where the lake stands, or stood when it was last known."""
    for key in ("stage", "stage_before"):
        if lake.get(key) in _RANK:
            return lake[key]
    return None


def describe(lake: Mapping[str, Any]) -> str:
    level = f"{lake['level_cm']:+.1f}".replace(".", ",")
    return (
        f"{lake.get('name', 'Lago')}: {_WORDS[lake['stage']]}. Altezza idrometrica {level} cm "
        f"(attenzione a {lake['attention_cm']:g} cm, allarme a {lake['alarm_cm']:g} cm; dati laghi.net)."
    )


def decide(now: str | None, before: str | None, *, issue_open: bool) -> str:
    """What to do with the issue, given where the highest lake stands and stood.

    ``now`` is ``None`` when no level could be read: nothing is done, a site
    that is down is not a lake that went down. ``before`` is ``None`` when the
    page online could not say: then an issue already open is left as it is.
    """
    if now is None:
        return NOTHING
    if now == lakes.NORMAL:
        return CLOSE if issue_open else NOTHING
    if not issue_open:
        return OPEN
    if before is not None and _RANK[now] > _RANK[before]:
        return WORSE
    return NOTHING


def reconcile(
    issues: GitHubIssues, *, owner: str, status: Mapping[str, Any] | None, previous: Mapping[str, Any] | None
) -> str:
    """Bring the issue in line with the lake. Raises ``FamilyLogError`` if GitHub refuses."""
    known = [lake for lake in _lakes(status) if lake.get("stage") in _RANK]
    if not known:
        return NOTHING
    worst = max(known, key=lambda lake: _RANK[lake["stage"]])
    earlier = [stage for stage in map(_stage, _lakes(previous)) if stage]
    before = max(earlier, key=_RANK.__getitem__) if earlier else None
    current = issues.issue_titled(ISSUE_TITLE, state="open", what="ricerca della segnalazione del lago")
    action = decide(worst["stage"], before, issue_open=current is not None)
    mention = f"@{owner} " if owner else ""
    if action == OPEN:
        body = (
            mention + describe(worst) + "\n\nIl pannello di controllo del calendario mostra il livello aggiornato. "
            "Questa segnalazione è scritta dal job e si chiude da sola quando il lago scende."
        )
        try:
            issues.create_issue(ISSUE_TITLE, body, what="apertura della segnalazione del lago", assignees=[owner] if owner else None)
        except FamilyLogError as exc:
            if not owner or "HTTP 422" not in str(exc):  # an organisation cannot be assigned an issue
                raise
            issues.create_issue(ISSUE_TITLE, body, what="apertura della segnalazione del lago")
    elif action == WORSE:
        issues.comment(int(current["number"]), mention + describe(worst), what="avviso del lago")
    elif action == CLOSE:
        number = int(current["number"])
        issues.comment(number, CLOSING_TEXT, what="chiusura della segnalazione del lago")
        issues.close_issue(number, what="chiusura della segnalazione del lago")
    return action
