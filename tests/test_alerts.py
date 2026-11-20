import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from casecal import sync
from casecal.alerts import (
    CLOSE,
    HORIZON_DAYS,
    ISSUE_TITLE,
    NEW,
    NOTHING,
    OPEN,
    REMIND,
    decide,
    needing_action,
    reconcile,
)
from casecal.config import Config, PropertyConfig
from casecal.family import FamilyEntry, entry_status
from casecal.familylog import FamilyLogError, FamilyState, GitHubIssues
from casecal.model import BOOKING, CLOSED, MissingProperty, PropertySnapshot, Snapshot, Stay
from casecal.sync import PreviousPage, SyncReport, alert_owner
from github_fake import TOKEN, FakeGitHub

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "entry_status.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 8, 4, 2, 17, tzinfo=timezone.utc)
TUESDAY, MONDAY = date(2026, 8, 4), date(2026, 8, 3)
WORKFLOW_TOKEN = "ghs_workflow-token-di-prova"


def house(stays, house_id="santa", start="2026-01-01", end="2027-01-01"):
    return PropertySnapshot(
        id=house_id, label=house_id.title(), fetched_at=NOW,
        covered_from=date.fromisoformat(start), covered_to=date.fromisoformat(end),
        stays=[Stay(date.fromisoformat(a), date.fromisoformat(b), CLOSED if kind == "c" else BOOKING)
               for a, b, kind, *_ in stays],
    )


def period(entry_id, start, end, prop="santa", who="Famiglia Rossi"):
    return FamilyEntry.from_dict({"id": entry_id, "prop": prop, "start": start, "end": end, "who": who, "by": "Paolo"})


# Guests from the 20th to the 25th of August; the first week of September is blocked.
SANTA = house([("2026-08-20", "2026-08-25", "b"), ("2026-09-01", "2026-09-08", "c")])
BLOCKED = period("e_blocked0001", "2026-09-02", "2026-09-05")
PENDING = period("e_pending0001", "2026-08-10", "2026-08-12")
CONFLICT = period("e_conflict001", "2026-08-22", "2026-08-24")
FAR = period("e_far00000001", "2026-12-10", "2026-12-12")  # for sale, but more than 90 days away
OVER = period("e_over0000001", "2026-07-01", "2026-07-03")  # for sale, and already gone


# --------------------------------------------------------------------------- where a period stands


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["why"])
def test_the_job_judges_a_period_as_the_page_does(case):
    calendar = house(FIXTURE["house"]["stays"], start=FIXTURE["house"]["from"], end=FIXTURE["house"]["to"])
    assert entry_status(period("e_case0000001", case["start"], case["end"]), calendar) == case["state"]


def test_a_period_of_a_house_that_was_not_read_cannot_be_judged():
    assert entry_status(PENDING, None) == "unknown"
    assert entry_status(PENDING, MissingProperty("santa", "Santa")) == "unknown"


def test_only_periods_not_yet_over_that_need_someone_are_waiting():
    waiting = needing_action([BLOCKED, PENDING, CONFLICT, FAR, OVER], {"santa": SANTA}, TUESDAY)
    assert waiting == {"e_pending0001": "pending", "e_conflict001": "conflict", "e_far00000001": "pending"}
    # A period that ends today is over: the house is left this morning.
    assert needing_action([PENDING], {"santa": SANTA}, date(2026, 8, 12)) == {}
    assert needing_action([PENDING], {"santa": SANTA}, date(2026, 8, 11)) == {"e_pending0001": "pending"}
    # A house that is not among those judged tonight says nothing either way.
    assert needing_action([PENDING, period("e_lake0000001", "2026-08-10", "2026-08-12", prop="vassena")],
                          {"santa": SANTA}, TUESDAY) == {"e_pending0001": "pending"}


# --------------------------------------------------------------------------- what to do tonight


@pytest.mark.parametrize(
    "waiting, before, soon, monday, issue_open, complete, action",
    [
        ({}, {}, False, False, False, True, NOTHING),
        ({}, {"a": "pending"}, False, False, True, True, CLOSE),          # everything got blocked
        ({}, {"a": "pending"}, False, False, True, False, NOTHING),       # ...or a house was not read: proves nothing
        ({"a": "pending"}, {}, True, False, False, True, OPEN),           # a period just written on nights for sale
        ({"a": "pending"}, {}, False, False, False, True, OPEN),          # however far away it is
        ({"a": "pending", "b": "pending"}, {"a": "pending"}, True, False, True, True, NEW),
        ({"a": "conflict"}, {"a": "pending"}, True, False, True, True, NEW),  # guests booked on top of it
        ({"a": "pending"}, {"a": "pending"}, True, False, True, True, NOTHING),
        ({"a": "pending"}, {"a": "pending"}, True, True, True, True, REMIND),
        ({"a": "pending"}, {"a": "pending"}, True, True, False, True, OPEN),   # the owner had closed it by hand
        ({"a": "pending"}, {"a": "pending"}, False, True, True, True, NOTHING),  # nothing begins within the horizon
        ({"a": "pending"}, None, True, False, False, True, NOTHING),      # the page online could not be read
        ({"a": "pending"}, None, True, True, False, True, OPEN),          # the Monday reminder does not need it
    ],
)
def test_what_is_done_with_the_alert(waiting, before, soon, monday, issue_open, complete, action):
    assert decide(waiting, before, soon=soon, monday=monday, issue_open=issue_open, complete=complete) == action


# --------------------------------------------------------------------------- the issue itself


def service():
    fake = FakeGitHub()
    fake.tokens = {WORKFLOW_TOKEN}
    return fake


def run(fake, entries, previous, today=TUESDAY, **options):
    return reconcile(
        GitHubIssues(WORKFLOW_TOKEN, "owner/repo", session=fake), owner="owner", entries=entries,
        houses={"santa": SANTA}, previous_entries=previous,
        previous_houses=None if previous is None else {"santa": SANTA}, today=today, **options,
    )


def test_a_new_period_on_nights_for_sale_opens_an_issue_for_the_owner():
    fake = service()
    outcome = run(fake, [BLOCKED, PENDING], previous=[BLOCKED])
    assert (outcome.action, outcome.waiting) == (OPEN, 1)
    issue = fake.issues[-1]
    assert issue["title"] == ISSUE_TITLE and issue["state"] == "open"
    assert issue["assignees"] == ["owner"]           # GitHub writes to the assignee...
    assert issue["body"].startswith("@owner ")       # ...and to whoever is mentioned
    # The repository is public: the text says where to look, never what is there.
    for private in ("Rossi", "Paolo", "santa", "Santa", "2026", "agosto", "10", "12"):
        assert private not in issue["body"], private


def test_the_life_of_an_alert():
    fake = service()
    assert run(fake, [PENDING], previous=[]).action == OPEN
    number = fake.issues[-1]["number"]

    # The next night nothing changed: not a word.
    calls = len(fake.calls)
    assert run(fake, [PENDING], previous=[PENDING]).action == NOTHING
    assert [call[0] for call in fake.calls[calls:]] == ["GET"]

    # Guests book on top of another period: a comment on the same issue.
    assert run(fake, [PENDING, CONFLICT], previous=[PENDING]).action == NEW
    assert len(fake.issues) == 1
    assert fake.comments[number][-1]["body"].startswith("@owner Ci sono nuovi periodi")

    # Monday: a reminder, because something begins within the horizon.
    assert run(fake, [PENDING, CONFLICT], previous=[PENDING, CONFLICT], today=MONDAY).action == REMIND
    assert f"entro {HORIZON_DAYS} giorni" in fake.comments[number][-1]["body"]
    assert len(fake.comments[number]) == 2

    # The manager blocks the dates and the other period is moved: the issue is closed with a last word.
    moved = period("e_conflict001", "2026-09-03", "2026-09-06")
    relieved = house([("2026-08-10", "2026-08-12", "c"), ("2026-08-20", "2026-08-25", "b"), ("2026-09-01", "2026-09-08", "c")])
    outcome = reconcile(GitHubIssues(WORKFLOW_TOKEN, "owner/repo", session=fake), owner="owner", entries=[PENDING, moved],
                        houses={"santa": relieved}, previous_entries=[PENDING, CONFLICT], previous_houses={"santa": SANTA},
                        today=TUESDAY)
    assert (outcome.action, outcome.waiting) == (CLOSE, 0)
    assert fake.issues[-1]["state"] == "closed" and fake.issues[-1]["state_reason"] == "completed"
    assert "@owner" not in fake.comments[number][-1]["body"]  # good news needs no ping

    # Later a new period needs blocking: a new issue, not the closed one.
    assert run(fake, [FAR], previous=[]).action == OPEN
    assert [issue["state"] for issue in fake.issues] == ["closed", "open"]
    assert set(fake.callers) == {WORKFLOW_TOKEN}


def test_a_period_far_away_is_announced_once_and_not_repeated_on_mondays():
    fake = service()
    assert run(fake, [FAR], previous=[]).action == OPEN
    assert run(fake, [FAR], previous=[FAR], today=MONDAY).action == NOTHING


def test_an_issue_left_open_is_not_closed_when_a_house_could_not_be_judged():
    fake = service()
    run(fake, [PENDING], previous=[])
    assert run(fake, [], previous=[PENDING], complete=False).action == NOTHING
    assert fake.issues[-1]["state"] == "open"


def test_when_the_page_online_cannot_be_read_nothing_counts_as_new():
    fake = service()
    assert run(fake, [PENDING], previous=None).action == NOTHING
    assert fake.issues == []
    assert run(fake, [PENDING], previous=None, today=MONDAY).action == OPEN  # the reminder still goes out


def test_an_owner_that_cannot_be_assigned_is_still_mentioned():
    fake = service()
    fake.assign_status = 422  # an organisation, for one
    assert run(fake, [PENDING], previous=[]).action == OPEN
    assert fake.issues[-1]["assignees"] == [] and fake.issues[-1]["body"].startswith("@owner ")


def test_a_refusal_from_github_is_an_error_that_names_no_token():
    fake = service()
    fake.status = 403
    with pytest.raises(FamilyLogError, match="permesso Issues") as caught:
        run(fake, [PENDING], previous=[])
    assert WORKFLOW_TOKEN not in str(caught.value)


# --------------------------------------------------------------------------- the nightly job

CONFIG = Config(
    site_id="owner/repo",
    properties=(
        PropertyConfig(id="santa", label="Santa", env="SANTA"),
        PropertyConfig(id="vassena", label="Vassena", env="VASSENA"),
    ),
)
VASSENA = house([], house_id="vassena")
LAKE = period("e_lake0000001", "2026-08-10", "2026-08-12", prop="vassena")


def tonight(monkeypatch, fake, entries, *, fresh=True, token=WORKFLOW_TOKEN, report=None, previous=None):
    monkeypatch.setattr(sync, "GitHubIssues", lambda token, repo, api: GitHubIssues(token, repo, api=api, session=fake))
    snapshot = Snapshot(generated_at=NOW, properties=[SANTA, VASSENA])
    report = report or SyncReport(fresh=["santa", "vassena"])

    def load_previous():
        if previous is None:
            raise AssertionError("the page online is only needed when something is waiting")
        return previous

    return alert_owner(
        CONFIG, snapshot, report, FamilyState(entries=entries, log={"kind": "github"}, warnings=[], fresh=fresh),
        token=token, repository="owner/repo", owner="owner", load_previous=load_previous, now=NOW,
    )


def test_the_job_opens_the_alert_with_the_workflows_own_token(monkeypatch):
    fake = service()
    online = PreviousPage(properties={"santa": SANTA, "vassena": VASSENA}, entries=[BLOCKED])
    assert tonight(monkeypatch, fake, [BLOCKED, PENDING], previous=online) == (OPEN, None)
    assert fake.issues[-1]["title"] == ISSUE_TITLE and set(fake.callers) == {WORKFLOW_TOKEN}


def test_nothing_waiting_means_the_page_online_is_not_even_fetched(monkeypatch):
    fake = service()
    assert tonight(monkeypatch, fake, [BLOCKED, OVER]) == (NOTHING, None)


def test_the_alert_is_left_alone_unless_the_log_was_read_tonight(monkeypatch):
    fake = service()
    assert tonight(monkeypatch, fake, [PENDING], fresh=False) == (None, None)
    assert tonight(monkeypatch, fake, [PENDING], token=None) == (None, None)
    assert fake.calls == []


def test_a_house_read_with_problems_is_not_judged_and_keeps_the_alert_open(monkeypatch):
    fake = service()
    online = PreviousPage(properties={"santa": SANTA, "vassena": VASSENA}, entries=[])
    assert tonight(monkeypatch, fake, [LAKE], previous=online) == (OPEN, None)
    # Tonight the lake house's bookings could not be read: its period cannot be called blocked or not.
    shaky = SyncReport(fresh=["santa", "vassena"], warnings={"vassena": ["dettagli delle prenotazioni non letti"]})
    assert tonight(monkeypatch, fake, [LAKE], report=shaky) == (NOTHING, None)
    assert fake.issues[-1]["state"] == "open"
    # And a house that was not read at all is not judged either.
    reused = SyncReport(fresh=["santa"], reused=["vassena"])
    assert tonight(monkeypatch, fake, [LAKE], report=reused) == (NOTHING, None)


def test_a_page_online_that_cannot_be_read_back_announces_nothing_new(monkeypatch):
    fake = service()
    assert tonight(monkeypatch, fake, [PENDING], previous=PreviousPage()) == (NOTHING, None)
    assert fake.issues == []


def test_a_failure_to_write_the_alert_is_reported_not_raised(monkeypatch):
    fake = service()
    fake.status = 401
    online = PreviousPage(properties={"santa": SANTA, "vassena": VASSENA}, entries=[])
    action, problem = tonight(monkeypatch, fake, [PENDING], previous=online)
    assert action is None and "avviso al proprietario non aggiornato" in problem and "token rifiutato" in problem
    assert WORKFLOW_TOKEN not in problem


def test_the_workflow_lets_the_job_write_the_alert_as_github_actions():
    workflow = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    assert "issues: write" in workflow
    # The workflow's own token, not a secret of the owner's: nobody is notified of what they do themselves.
    assert "ALERTS_TOKEN: ${{ github.token }}" in workflow
    assert "ALERTS_TOKEN: ${{ secrets." not in workflow
