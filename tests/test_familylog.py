from datetime import datetime, timezone

import pytest
import requests

from casecal import sync
from casecal.config import Config, PropertyConfig
from casecal.crypto import derive_key, open_envelope, site_salt
from casecal.family import FamilyEntry, decode_event, encode_event
from casecal.familylog import (
    ISSUE_TITLE,
    FamilyLogError,
    FamilyState,
    GitHubLog,
    LogComment,
    read_family_state,
    since_for,
    token_problem,
)
from casecal.model import Snapshot
from casecal.site import extract_envelope
from casecal.sync import PreviousPage, family_state, previous_from_site, publish
from github_fake import SECONDARY_LIMIT, TOKEN, FakeGitHub

NOW = datetime(2026, 8, 15, 2, 17, tzinfo=timezone.utc)
FAMILY, OLD = "nebbia-quercia-sentiero-42", "vecchia-password-di-casa"
CONFIG = Config(
    site_id="owner/repo",
    properties=(
        PropertyConfig(id="santa", label="Santa", env="SANTA"),
        PropertyConfig(id="vassena", label="Vassena", env="VASSENA"),
    ),
)
KEY = derive_key(FAMILY, site_salt("owner/repo"))
OLD_KEY = derive_key(OLD, site_salt("owner/repo"))


def entry(entry_id="e_k3j9x2m1q8", **overrides):
    raw = {"id": entry_id, "prop": "santa", "start": "2027-07-03", "end": "2027-07-10",
           "who": "Marco", "note": "", "by": "Marco", "at": "2026-11-20T12:30:00Z"}
    raw.update(overrides)
    return raw


def log_on(fake):
    return GitHubLog(TOKEN, "owner/repo", session=fake)


def test_the_log_issue_is_created_once_and_locked():
    fake = FakeGitHub(issues=[{"number": 3, "title": "Altro"}])
    number = log_on(fake).find_or_create_issue()
    assert number == 41
    assert fake.issues[-1]["title"] == ISSUE_TITLE
    assert fake.locked == [41]  # on a public repository an open issue would accept anyone's comments
    assert log_on(fake).find_or_create_issue() == 41  # found the second time, not created again
    assert [call[0] for call in fake.calls].count("POST") == 1


def test_the_oldest_issue_with_our_title_wins_and_pull_requests_are_ignored():
    fake = FakeGitHub(issues=[
        {"number": 2, "title": ISSUE_TITLE, "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/2"}},
        {"number": 5, "title": ISSUE_TITLE},
        {"number": 9, "title": ISSUE_TITLE},
    ])
    assert log_on(fake).find_or_create_issue() == 5
    assert fake.calls[0][2]["state"] == "all"  # a log closed by mistake is still the log


def test_a_log_left_unlocked_is_locked_again():
    fake = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE, "locked": False}])
    assert log_on(fake).find_or_create_issue() == 7 and fake.locked == [7]
    fake = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE, "locked": True}])
    assert log_on(fake).find_or_create_issue() == 7 and fake.locked == []


def test_a_log_that_cannot_be_locked_is_still_read_and_the_run_says_so():
    fake = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE, "locked": False}], comments={7: [
        encode_event(KEY, {"t": "set", **entry()}),
    ]})
    fake.lock_status = 403
    state = read(fake)
    assert [e.id for e in state.entries] == ["e_k3j9x2m1q8"] and state.log["issue"] == 7
    assert len(state.warnings) == 1 and "blocco del registro" in state.warnings[0]
    assert "aperta ai commenti di chiunque" in state.warnings[0]


def test_a_log_too_long_to_read_whole_is_an_error_not_a_silent_cut(monkeypatch):
    monkeypatch.setattr("casecal.familylog.MAX_PAGES", 2)
    bodies = [{"id": 5000 + n, "body": f"c{n}"} for n in range(200)]
    fake = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE}], comments={7: bodies})
    with pytest.raises(FamilyLogError, match="troppi elementi"):
        log_on(fake).comments(7)
    fake.comments[7].pop()  # 199 fit in two pages
    assert len(log_on(fake).comments(7)) == 199


def test_comments_are_read_across_pages_oldest_first():
    bodies = [{"id": 5000 - n, "body": f"c{n}"} for n in range(230)]  # served newest first
    fake = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE}], comments={7: bodies})
    comments = log_on(fake).comments(7)
    assert len(comments) == 230
    assert [c.id for c in comments] == sorted(c.id for c in comments)
    assert [call[2]["page"] for call in fake.calls] == [1, 2, 3]


@pytest.mark.parametrize(
    "fake, message",
    [
        (FakeGitHub(status=401), "token rifiutato"),
        (FakeGitHub(status=403), "permesso Issues"),
        (FakeGitHub(status=404), "permesso Issues"),
        (FakeGitHub(status=403, headers={"x-ratelimit-remaining": "0"}), "limite di richieste"),
        (FakeGitHub(status=403, refusal=SECONDARY_LIMIT), "limite di richieste"),
        (FakeGitHub(status=403, refusal=ValueError("not json")), "permesso Issues"),
        (FakeGitHub(status=429), "limite di richieste"),
        (FakeGitHub(status=410), "Issues sono disattivate"),
        (FakeGitHub(status=500), "HTTP 500"),
        (FakeGitHub(error=requests.ConnectionError("api.github.com unreachable")), "non raggiungibile"),
        # An answer of a shape the API has never had must not crash the nightly job.
        (FakeGitHub(answer={"message": "not a list"}), "risposta inattesa"),
        (FakeGitHub(answer=["not an issue"]), "risposta inattesa"),
        (FakeGitHub(answer=ValueError("not json")), "risposta inattesa"),
    ],
)
def test_failures_are_explained_without_leaking_the_token(fake, message):
    with pytest.raises(FamilyLogError, match=message) as caught:
        log_on(fake).find_or_create_issue()
    assert TOKEN not in str(caught.value)


def test_a_malformed_repository_name_is_refused():
    with pytest.raises(FamilyLogError, match="non valido"):
        GitHubLog(TOKEN, "not a repo")


def test_comments_of_an_unexpected_shape_are_a_failed_read():
    fake = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE}], comments={7: [{"body": "no id"}]})
    with pytest.raises(FamilyLogError, match="lettura del registro: risposta inattesa"):
        log_on(fake).comments(7)


def test_only_a_fine_grained_token_may_be_handed_to_the_page():
    assert token_problem("github_pat_ESEMPIO") is None
    for token in ("ghp_classico-di-prova", "gho_di-prova", "token-senza-prefisso"):
        problem = token_problem(token)
        assert "fine-grained" in problem and token not in problem


def test_the_since_filter_starts_before_the_last_comment_read():
    assert since_for(LogComment(1, "x", "2026-11-20T12:30:00Z")) == "2026-11-20T11:30:00Z"
    assert since_for(LogComment(1, "x", "2027-01-01T00:10:00Z")) == "2026-12-31T23:10:00Z"
    # No comment yet, or a time that cannot be read: no filter, the page asks for everything.
    assert since_for(None) is None
    assert since_for(LogComment(1, "x", "")) is None
    assert since_for(LogComment(1, "x", "ieri")) is None


def nothing_published() -> FamilyState:
    return FamilyState(entries=[], log=None, warnings=[])


def read(fake, **overrides):
    options = dict(key=KEY, old_key=None, token=TOKEN, known_houses={"santa", "vassena"}, fallback=nothing_published)
    options.update(overrides)
    return read_family_state(log_on(fake), **options)


def test_the_state_is_the_replayed_log():
    fake = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE}], comments={7: [
        encode_event(KEY, {"t": "set", **entry()}),
        "un commento qualsiasi",
        encode_event(KEY, {"t": "set", **entry("e_second0001", prop="vassena", who="Nonna")}),
        encode_event(KEY, {"t": "set", **entry(who="Marco e Giulia")}),
        encode_event(KEY, {"t": "del", "id": "e_second0001"}),
    ]})
    state = read(fake)
    assert [(e.id, e.who) for e in state.entries] == [("e_k3j9x2m1q8", "Marco e Giulia")]
    assert state.warnings == []
    assert state.log == {
        "kind": "github", "api": "https://api.github.com", "repo": "owner/repo",
        "issue": 7, "token": TOKEN,
        "cursor": 1004,                    # the last comment read: the page asks only for newer ones...
        "since": "2026-11-20T11:04:00Z",   # ...and tells GitHub to skip what is older than that one
    }


def test_an_empty_log_turns_editing_on_with_nothing_in_it():
    state = read(FakeGitHub())
    assert state.entries == [] and state.log["issue"] == 40
    assert state.log["cursor"] == 0 and state.log["since"] is None


def test_entries_of_a_house_no_longer_configured_are_not_published():
    fake = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE}], comments={7: [
        encode_event(KEY, {"t": "set", **entry(prop="montagna")}),
    ]})
    assert read(fake).entries == []


def test_a_log_that_cannot_be_read_keeps_what_is_already_published():
    published = FamilyEntry.from_dict(entry())
    state = read(FakeGitHub(status=401), fallback=lambda: FamilyState([published], {"kind": "github", "issue": 7}, []))
    assert state.entries == [published] and state.log == {"kind": "github", "issue": 7}
    assert len(state.warnings) == 1 and "token rifiutato" in state.warnings[0]
    assert TOKEN not in state.warnings[0]


def test_events_under_another_key_are_reported_not_silently_dropped():
    fake = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE}], comments={7: [
        encode_event(OLD_KEY, {"t": "set", **entry()}),
        encode_event(KEY, {"t": "set", **entry("e_second0001", who="Nonna")}),
    ]})
    state = read(fake)
    assert [e.id for e in state.entries] == ["e_second0001"]
    assert "1 voci" in state.warnings[0] and "OLD_FAMILY_PASSPHRASE" in state.warnings[0]


def test_a_changed_passphrase_is_migrated_with_a_snapshot():
    fake = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE}], comments={7: [
        encode_event(OLD_KEY, {"t": "set", **entry()}),
        encode_event(OLD_KEY, {"t": "set", **entry("e_second0001", who="Nonna")}),
        encode_event(KEY, {"t": "del", "id": "e_second0001"}),
    ]})
    state = read(fake, old_key=OLD_KEY)
    assert [e.id for e in state.entries] == ["e_k3j9x2m1q8"]
    assert state.warnings == []
    snapshot = fake.comments[7][-1]
    assert state.log["cursor"] == snapshot["id"] and state.log["since"] == "2026-11-20T11:03:00Z"
    assert decode_event(KEY, snapshot["body"])["t"] == "snap"  # written under the new key...
    assert decode_event(OLD_KEY, snapshot["body"]) is None

    # ...so the next run needs the old passphrase no longer, and warns about nothing.
    again = read(fake)
    assert [e.id for e in again.entries] == ["e_k3j9x2m1q8"] and again.warnings == []
    assert len(fake.comments[7]) == 4  # and no second snapshot is written


# --------------------------------------------------------------------------- the nightly job


def test_without_a_token_the_page_stays_read_only():
    state = family_state(CONFIG, token=None, repository="owner/repo", family_passphrase=FAMILY,
                         old_family_passphrase=None, load_previous=PreviousPage)
    assert (state.entries, state.log, state.warnings) == ([], None, [])


def test_the_job_reads_the_log_with_the_family_key(monkeypatch):
    fake = FakeGitHub(issues=[{"number": 7, "title": ISSUE_TITLE}], comments={7: [
        encode_event(KEY, {"t": "set", **entry()}),
    ]})
    monkeypatch.setattr(sync, "GitHubLog", lambda token, repo, api: GitHubLog(token, repo, api=api, session=fake))
    state = family_state(CONFIG, token=TOKEN, repository="owner/repo", family_passphrase=FAMILY,
                         old_family_passphrase=None, load_previous=PreviousPage)
    assert [e.who for e in state.entries] == ["Marco"] and state.log["issue"] == 7


def test_a_classic_token_is_never_published_and_the_entries_stay(monkeypatch):
    def unreachable(*args, **kwargs):
        raise AssertionError("a token of the wrong kind must not even be tried")

    monkeypatch.setattr(sync, "GitHubLog", unreachable)
    published = PreviousPage(entries=[FamilyEntry.from_dict(entry())],
                             log={"kind": "github", "issue": 7, "token": "ghp_classic"})
    state = family_state(CONFIG, token="ghp_classic", repository="owner/repo", family_passphrase=FAMILY,
                         old_family_passphrase=None, load_previous=lambda: published)
    assert [e.id for e in state.entries] == ["e_k3j9x2m1q8"]
    assert state.log is None  # not even the copy the page online still carries
    assert len(state.warnings) == 1 and "fine-grained" in state.warnings[0]
    assert "ghp_classic" not in state.warnings[0]


def test_a_job_without_a_repository_keeps_the_published_entries():
    published = PreviousPage(entries=[FamilyEntry.from_dict(entry())], log={"kind": "github", "issue": 7})
    state = family_state(CONFIG, token=TOKEN, repository=None, family_passphrase=FAMILY,
                         old_family_passphrase=None, load_previous=lambda: published)
    assert [e.id for e in state.entries] == ["e_k3j9x2m1q8"] and state.log == {"kind": "github", "issue": 7}
    assert "non configurato" in state.warnings[0]


def test_entries_and_log_settings_travel_encrypted_with_the_calendar(tmp_path, monkeypatch):
    written = entry(who="Ermenegilda", by="Ermenegilda")
    snapshot = Snapshot(generated_at=NOW, properties=[], entries=[FamilyEntry.from_dict(written)],
                        log={"kind": "github", "api": "https://api.github.com", "repo": "owner/repo",
                             "issue": 7, "token": TOKEN, "cursor": 1000, "since": "2026-11-20T11:00:00Z"})
    index = publish(snapshot, passphrase=FAMILY, site_id="owner/repo", out_dir=tmp_path)
    page = index.read_text(encoding="utf-8")
    assert TOKEN not in page and "Ermenegilda" not in page  # the token is only ever published encrypted
    envelope = extract_envelope(page)
    family, figures = open_envelope(envelope, passphrase=FAMILY)
    assert family["entries"] == [written] and family["log"]["token"] == TOKEN
    assert TOKEN not in str(figures) and "Ermenegilda" not in str(figures)  # in the calendar's blob only

    monkeypatch.setattr(sync, "fetch_published_envelope", lambda url: envelope)
    previous = previous_from_site("https://x.example", passphrase=FAMILY)
    assert previous.entries == [FamilyEntry.from_dict(written)]
    assert previous.log["issue"] == 7 and previous.log["since"] == "2026-11-20T11:00:00Z"
