"""Where a pass that changes at every use is kept between one run and the next.

Daikin's service renews its pass each time it is used, and voids the one
before. The job keeps nothing of its own from one run to the next, and cannot
rewrite its secrets; so the newest pass is kept, encrypted with the page's
password, in the text of an issue of this repository: the same place, and the
same protection, as the family's own entries.

The issue is public like the repository. What it holds opens only with the
page's password, and a text that was tampered with opens with nothing.
"""

from __future__ import annotations

import json
from typing import Mapping

from .crypto import decrypt_json, derive_key, encrypt_json, site_salt
from .devices import DeviceError
from .familylog import DEFAULT_API, FamilyLogError, GitHubIssues

ISSUE_TITLE = "Stato del pannello di controllo (dati cifrati: non modificare)"
_AAD = "casecal/v1/pass/daikin"
_NOTE = (
    "Qui il job conserva, cifrata con la password della pagina, l'autorizzazione che Daikin "
    "rinnova a ogni lettura. Non modificare e non chiudere: si aggiorna da sola.\n\n"
)


class IssuePass:
    """The pass, in the text of an issue."""

    def __init__(self, issues: GitHubIssues, key: bytes) -> None:
        self._issues, self._key = issues, key
        self._number: int | None = None

    def load(self) -> str | None:
        try:
            issue = self._issues.issue_titled(ISSUE_TITLE, state="open", what="stato del pannello")
        except FamilyLogError as exc:
            raise DeviceError(f"Daikin Onecta: {exc}") from None
        if issue is None:
            return None
        self._number = int(issue["number"])
        try:
            blob = json.loads(str(issue.get("body") or "").rsplit("\n", 1)[-1])
            held = decrypt_json(self._key, blob, aad=_AAD)
            return str(held["refresh_token"]) or None
        except Exception:  # noqa: BLE001 - written by hand, or with another password: start again from the secret
            return None

    def save(self, refresh_token: str) -> None:
        body = _NOTE + json.dumps(encrypt_json(self._key, {"refresh_token": refresh_token}, aad=_AAD, pad=64), separators=(",", ":"))
        try:
            if self._number is None:
                self._number = self._issues.create_issue(ISSUE_TITLE, body, what="stato del pannello")
            else:
                self._issues.set_issue_body(self._number, body, what="stato del pannello")
        except FamilyLogError as exc:
            # The pass just used is void and the new one is lost: said loudly, it needs a hand.
            raise DeviceError(
                f"Daikin Onecta: nuova autorizzazione non salvata ({exc}): va rifatta (python -m casecal daikin-login)"
            ) from None


def issue_pass(environ: Mapping[str, str]) -> IssuePass:
    token = environ.get("ALERTS_TOKEN", "").strip()
    repository = environ.get("GITHUB_REPOSITORY", "").strip()
    passphrase = environ.get("FAMILY_PASSPHRASE", "")
    if not token or not repository or not passphrase:
        raise DeviceError("Daikin Onecta: servono ALERTS_TOKEN e GITHUB_REPOSITORY per conservare l'autorizzazione")
    try:
        issues = GitHubIssues(token, repository, api=environ.get("GITHUB_API_URL", "").strip() or DEFAULT_API)
    except FamilyLogError as exc:
        raise DeviceError(f"Daikin Onecta: {exc}") from None
    return IssuePass(issues, derive_key(passphrase, site_salt("pass:" + repository)))
