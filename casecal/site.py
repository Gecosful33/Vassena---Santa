"""Build the single self-contained HTML page and read back a published one."""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import re
from pathlib import Path
from typing import Any, Mapping

import requests

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
        "__SHORT__": html.escape(SHORT_NAME, quote=True),
        "__THEME__": _colours("--page")[0],
        "__THEME_DARK__": _colours("--page")[-1],
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


SHORT_NAME = "Case"  # under the icon, where there is room for a word
# The pictures the site is installed with, by file and by side in pixels. The last is the one
# an iPhone asks for by name; the others are those the manifest lists.
ICONS = {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180}
_LISTED_ICONS = ("icon-192.png", "icon-512.png")
KEPT_OFFLINE = ["./", "manifest.webmanifest", *ICONS]  # what sw.js keeps for when there is no connection


def _colours(name: str) -> list[str]:
    """The values the stylesheet gives a colour, the light scheme's first: the page's palette is written there alone."""
    found = re.findall(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})\s*;", _read("style.css"))
    if not found:
        raise SiteError(f"the stylesheet no longer defines {name}")
    return found


def icon(size: int) -> bytes:
    """The picture the page has on a phone's home screen, as a PNG: a house, in the colours of the two houses.

    Drawn here, so that no image file is kept in the repository.
    """
    from PIL import Image, ImageDraw  # only the job that writes the site draws anything

    scale = 4  # drawn large and reduced, for smooth edges
    image = Image.new("RGB", (size * scale, size * scale), _colours("--series-2")[0])
    draw = ImageDraw.Draw(image)
    u = size * scale / 100
    draw.polygon([(50 * u, 22 * u), (82 * u, 50 * u), (18 * u, 50 * u)], fill="#ffffff")
    draw.rectangle([27 * u, 48 * u, 73 * u, 78 * u], fill="#ffffff")
    draw.rectangle([44 * u, 58 * u, 56 * u, 78 * u], fill=_colours("--series-1")[0])
    drawn = io.BytesIO()
    image.resize((size, size), Image.LANCZOS).save(drawn, "PNG")
    return drawn.getvalue()


def manifest(title: str) -> dict[str, Any]:
    """What a phone needs to keep the page on its home screen and open it like an app, without the browser's bars."""
    page = _colours("--page")[0]
    return {
        "name": title,
        "short_name": SHORT_NAME,
        "lang": "it",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": page,
        "theme_color": page,
        "icons": [
            {"src": name, "sizes": f"{ICONS[name]}x{ICONS[name]}", "type": "image/png", "purpose": "any"}
            for name in _LISTED_ICONS
        ],
    }


def worker() -> str:
    """sw.js, told which files to keep: the list is this module's, so that it cannot name a file the site lacks."""
    files = json.dumps(KEPT_OFFLINE)
    kept = "casecal-" + hashlib.sha256(files.encode("utf-8")).hexdigest()[:8]  # a new list is a new store
    return _read("sw.js").replace("__FILES__", files).replace("__KEPT__", kept)


def write_site(out_dir: str | Path, page: str, *, title: str) -> Path:
    """Write the page and what goes with it; ``title`` is the name the page was rendered with."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8", newline="\n")
    # Nothing here is private: a name, and a drawing of a house.
    (out / "manifest.webmanifest").write_text(json.dumps(manifest(title), ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "sw.js").write_text(worker(), encoding="utf-8", newline="\n")
    for name, size in ICONS.items():
        (out / name).write_bytes(icon(size))
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    return out / "index.html"
