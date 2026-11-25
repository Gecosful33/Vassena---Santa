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
    """The picture the page has on a phone's home screen, as a PNG: the white house with the yellow shutters.

    Drawn here, so that no image file is kept in the repository; the measures are hundredths of the side.
    """
    from PIL import Image, ImageDraw  # only the job that writes the site draws anything

    scale = 4  # drawn large and reduced, for smooth edges
    side = size * scale
    u = side / 100
    wall, shade, slab = "#f8f7f2", "#d9dbd8", "#ffffff"
    yellow, slat, stone, joint = "#f7c912", "#dba900", "#6b675f", "#8d887e"

    image = Image.new("RGB", (side, side))
    draw = ImageDraw.Draw(image)

    def box(left: float, top: float, right: float, bottom: float, fill: str) -> None:
        draw.rectangle([left * u, top * u, right * u, bottom * u], fill=fill)

    def shutter(left: float, top: float, right: float, bottom: float, leaves: int) -> None:
        box(left, top, right, bottom, yellow)
        y = top + 1.6
        while y < bottom - 0.5:
            box(left, y, right, y + 0.35, slat)
            y += 1.6
        for leaf in range(1, leaves):
            x = left + (right - left) * leaf / leaves
            box(x - 0.25, top, x + 0.25, bottom, slat)

    def pillar(left: float, top: float, right: float, bottom: float) -> None:
        box(left, top, right, bottom, stone)
        y, row = top + 2.2, 0
        while y < bottom:
            box(left, y, right, y + 0.3, joint)
            x = left + (1.2 if row % 2 else 2.6)
            if x < right - 0.5:
                box(x, y - 2.2, x + 0.3, y, joint)
            y, row = y + 2.2, row + 1

    box(0, 74, 100, 100, "#4c8c2b")  # the lawn, mown in stripes that meet behind the house
    for stripe in range(-8, 9, 2):
        draw.polygon([(50 * u, 40 * u), ((50 + stripe * 14) * u, 100 * u), ((50 + (stripe + 1) * 14) * u, 100 * u)], fill="#66a83a")
    for y in range(int(74 * u)):  # the sky, paler towards the roofs
        t = min(1.0, y / (60 * u))
        draw.line([(0, y), (side, y)], fill=(int(36 + 70 * t), int(112 + 62 * t), int(214 + 22 * t)))
    draw.polygon([(0, 30 * u), (10 * u, 22 * u), (24 * u, 34 * u), (30 * u, 74 * u), (0, 74 * u)], fill="#8a6a3a")  # the hills
    draw.polygon([(100 * u, 40 * u), (90 * u, 36 * u), (76 * u, 74 * u), (100 * u, 74 * u)], fill="#7d7a34")

    box(5, 27, 17, 72, wall)  # the wing on the left
    shutter(8, 31, 14, 42, 1)
    shutter(8, 57, 14, 64, 1)
    box(17, 20, 63, 46, wall)  # upstairs, over the porch
    shutter(19.5, 28, 60.5, 44, 3)
    box(17, 51, 63, 72, shade)  # the porch, in the shadow of the balcony
    shutter(19.5, 54, 60.5, 72, 4)
    box(67, 21, 96, 74, wall)  # the block on the right
    shutter(71, 29, 81, 44, 1)
    shutter(85, 29, 94, 44, 1)
    shutter(72, 56, 93, 63, 2)
    box(5, 45, 96, 50.5, slab)  # the balcony, from end to end
    box(5, 50.5, 96, 51.3, "#b9bcb9")
    pillar(14.5, 22, 18, 72)
    pillar(62, 11, 67.5, 74)
    box(4, 26.2, 15, 27.4, slab)  # the roofs' edges
    box(16, 19.2, 64, 20.4, slab)
    box(66.5, 20.2, 97, 21.4, slab)
    draw.rounded_rectangle([7 * u, 69 * u, 64 * u, 77 * u], radius=3 * u, fill="#27491f")  # the hedge
    draw.ellipse([86 * u, 66 * u, 104 * u, 80 * u], fill="#3d6e25")

    drawn = io.BytesIO()
    image.resize((size, size), Image.LANCZOS).save(drawn, "PNG", optimize=True)
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
