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
from PIL import Image, ImageDraw

WEB_DIR = Path(__file__).parent / "web"
SCRIPTS = ("crypto.js", "logic.js", "store.js", "app.js")
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
    (by hash). The only host the page may talk to is the GitHub API, where the
    family's own entries are kept as ciphertext; it can load nothing from
    anywhere, even if a scraped label carried markup.
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
            "connect-src https://api.github.com",
            "img-src data: 'self'",  # the houses' photos, and the icon the page is installed with
            "manifest-src 'self'",
            "worker-src 'self'",  # sw.js, which keeps the page for when there is no connection
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


ICON_BACKGROUND, ICON_ROOF, ICON_WALL, ICON_DOOR = "#eb6834", "#ffffff", "#ffffff", "#6250d6"


def icon(size: int) -> Image.Image:
    """The picture the page has on a phone's home screen: a house, drawn here so that no file is kept."""
    scale = 4  # drawn large and reduced, for smooth edges
    side = size * scale
    image = Image.new("RGB", (side, side), ICON_BACKGROUND)
    draw = ImageDraw.Draw(image)
    u = side / 100
    draw.polygon([(50 * u, 22 * u), (82 * u, 50 * u), (18 * u, 50 * u)], fill=ICON_ROOF)
    draw.rectangle([27 * u, 48 * u, 73 * u, 78 * u], fill=ICON_WALL)
    draw.rectangle([44 * u, 58 * u, 56 * u, 78 * u], fill=ICON_DOOR)
    return image.resize((size, size), Image.LANCZOS)


def manifest(title: str) -> dict[str, Any]:
    """What a phone needs to keep the page on its home screen and open it like an app, without the browser's bars."""
    return {
        "name": title,
        "short_name": "Case",
        "lang": "it",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": "#f9f9f7",
        "theme_color": "#f9f9f7",
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        ],
    }


def write_site(out_dir: str | Path, page: str, *, title: str = "Calendario case") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8", newline="\n")
    # Nothing here is private: a name, and a drawing of a house.
    (out / "manifest.webmanifest").write_text(json.dumps(manifest(title), ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "sw.js").write_text(_read("sw.js"), encoding="utf-8", newline="\n")
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        icon(size).save(out / name, "PNG", optimize=True)
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    return out / "index.html"
