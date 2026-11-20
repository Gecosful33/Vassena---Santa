"""Build the single self-contained HTML page and read back a published one."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any, Mapping

import requests

WEB_DIR = Path(__file__).parent / "web"
SCRIPTS = ("crypto.js", "logic.js", "app.js")
_ENVELOPE_RE = re.compile(
    r'<script type="application/json" id="casecal-data">(.*?)</script>', re.DOTALL
)


class SiteError(RuntimeError):
    """The page could not be assembled safely."""


def _read(name: str) -> str:
    # Normalise line endings: the CSP hashes below are computed over these exact
    # bytes and browsers hash the text after CRLF has been folded to LF.
    return (WEB_DIR / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def _csp_hash(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def _guard_inline(kind: str, text: str) -> None:
    lowered = text.lower()
    closing = f"</{kind}"
    if closing in lowered or "<!--" in lowered:
        raise SiteError(f"inline {kind} contains a sequence that would break out of its element")


def render_page(envelope: Mapping[str, Any], *, title: str) -> str:
    """Inline CSS, JS and the encrypted envelope into one HTML document.

    The Content-Security-Policy allows exactly this script and this stylesheet
    (by hash) and no network access at all, so the page cannot load or send
    anything even if a scraped label carried markup.
    """
    css = _read("style.css")
    script = "\n".join(_read(name) for name in SCRIPTS)
    _guard_inline("style", css)
    _guard_inline("script", script)

    data = json.dumps(envelope, separators=(",", ":"))
    _guard_inline("script", data)

    csp = "; ".join(
        [
            "default-src 'none'",
            f"script-src '{_csp_hash(script)}'",
            f"style-src '{_csp_hash(css)}'",
            "img-src data:",
            "base-uri 'none'",
            "form-action 'none'",
        ]
    )
    page = _read("page.html")
    replacements = {
        "__CSP__": html.escape(csp, quote=True),
        "__TITLE__": html.escape(title, quote=True),
        "__CSS__": css,
        "__ENVELOPE__": data,
        "__SCRIPT__": script,
    }
    for marker in replacements:
        if page.count(marker) != 1:
            raise SiteError(f"template marker {marker} must appear exactly once")
    # Single pass, so that text inserted for one marker is never rescanned for another.
    return re.sub("|".join(map(re.escape, replacements)), lambda m: replacements[m.group(0)], page)


def extract_envelope(page: str) -> dict[str, Any] | None:
    match = _ENVELOPE_RE.search(page)
    if not match:
        return None
    try:
        envelope = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return envelope if isinstance(envelope, dict) else None


def fetch_published_envelope(site_url: str, *, timeout: float = 20.0) -> dict[str, Any] | None:
    """Envelope of the page currently online, or ``None`` if there is none.

    The published page doubles as the job's only state: when one portal fails,
    the last good data for that house is recovered from here.
    """
    try:
        response = requests.get(
            site_url.rstrip("/") + "/index.html",
            timeout=timeout,
            headers={"Cache-Control": "no-cache"},
        )
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    response.encoding = "utf-8"
    return extract_envelope(response.text)


def write_site(out_dir: str | Path, page: str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8", newline="\n")
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    return out / "index.html"
