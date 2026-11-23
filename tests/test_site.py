import base64
import hashlib
import html
import re
from datetime import datetime, timezone

import pytest

from casecal import site
from casecal.crypto import build_envelope, open_envelope, site_salt
from casecal.demo import DEMO_PASSPHRASE, demo_snapshot
from casecal.site import SiteError, extract_envelope, render_page, write_site

NOW = datetime(2026, 8, 15, 2, 17, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def built():
    snapshot = demo_snapshot(NOW)
    envelope = build_envelope(
        passphrase=DEMO_PASSPHRASE,
        salt=site_salt("test"),
        family_payload=snapshot.family_payload(),
        owner_payload=snapshot.owner_payload(),
        iterations=100_000,
    )
    return snapshot, envelope, render_page(envelope, title="Calendario case")


def _csp(page: str) -> str:
    match = re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', page)
    return html.unescape(match.group(1))


def _sha256(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def test_the_envelope_survives_embedding(built):
    _, envelope, page = built
    assert extract_envelope(page) == envelope


def test_the_page_holds_only_ciphertext(built):
    snapshot, _, page = built
    # Everything outside the envelope is static code, so labels and amounts of the
    # snapshot can only appear if encryption was skipped.
    for prop in snapshot.properties:
        assert prop.label not in page
        assert prop.unit not in page
    assert snapshot.properties[0].stays[0].start.isoformat() not in page
    assert DEMO_PASSPHRASE not in page


def test_the_csp_pins_exactly_the_inlined_code(built):
    _, _, page = built
    csp = _csp(page)
    script = re.search(r"<script>(.*?)</script>", page, re.DOTALL).group(1)
    style = re.search(r"<style>(.*?)</style>", page, re.DOTALL).group(1)
    assert f"script-src '{_sha256(script)}'" in csp
    assert f"style-src '{_sha256(style)}'" in csp
    assert "default-src 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    # The family's entries live on GitHub as ciphertext: that API is the only host allowed.
    assert "connect-src https://api.github.com;" in csp
    assert csp.count("https://") == 1


def test_the_page_loads_nothing_from_the_network(built):
    _, _, page = built
    assert not re.search(r"""(?:src|href|action)\s*=\s*["']?(?:https?:)?//""", page)
    assert "@import" not in page
    assert "noindex" in page and 'name="referrer" content="no-referrer"' in page


def test_the_code_never_parses_strings_as_markup():
    script = "".join(site._read(name) for name in site.SCRIPTS)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script


# --------------------------------------------------------------------------- colours
#
# The eight hues of the reference palette, light and dark. Their order is what keeps
# neighbouring series apart for colour-blind readers, so it is fixed here: violet first
# (the first house), orange second, blue moved to the seventh slot.

PALETTE = {
    "light": ["#6250d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#2a78d6", "#e34948"],
    "dark": ["#9085e9", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#3987e5", "#e66767"],
}


def _series(css: str) -> dict[str, list[str]]:
    light, dark = css.split("@media (prefers-color-scheme: dark)", 1)
    dark = dark.split("\n}\n", 1)[0]
    return {
        mode: [value for _, value in sorted(re.findall(r"--series-(\d):\s*(#[0-9a-f]{6});", block))]
        for mode, block in (("light", light), ("dark", dark))
    }


def test_the_first_house_is_violet_and_the_palette_keeps_its_validated_order():
    css = site._read("style.css")
    assert _series(css) == PALETTE
    slots = re.findall(r"^\.p(\d) \{ --hue: var\(--series-(\d)\); \}$", css, re.MULTILINE)
    assert slots == [(str(n), str(n + 1)) for n in range(8)]  # house n wears slot n + 1


def test_text_on_a_series_colour_is_a_token_with_a_value_for_each_mode():
    css = site._read("style.css")
    assert re.findall(r"--on-series:\s*(#[0-9a-f]{6});", css) == ["#ffffff", "#0b0b0b"]  # light, then dark
    # Nothing writes white straight onto a series colour any more.
    assert not re.search(r"background: var\(--hue\); color: #", css)


# --------------------------------------------------------------------------- class names
#
# The stylesheet is one flat list of class names. Using the same word for two different
# things once turned the monthly statistics table into the calendar's grid of months
# (both were "months"), with header, rows and totals side by side.

def _own_layout_rules(css: str) -> set[str]:
    """Classes with a rule of their own (selector exactly ``.name``) that lays content out."""
    found = set()
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)):
        if not re.search(r"\b(?:display|position|grid-template\w*|flex\w*)\s*:", body):
            continue
        for selector in selectors.split(","):
            match = re.fullmatch(r"\.([A-Za-z][\w-]*)", selector.strip())
            if match:
                found.add(match.group(1))
    return found


def _class_expressions(script: str) -> list[tuple[str, str]]:
    """``(tag, class expression)`` for every ``el("tag", { ... class: ... })`` in the script."""
    found = []
    for match in re.finditer(r'\bel\("(\w+)",\s*\{', script):
        depth, end = 0, match.end() - 1
        while end < len(script):
            depth += {"{": 1, "}": -1}.get(script[end], 0)
            if depth == 0:
                break
            end += 1
        attributes = script[match.end():end]
        start = re.search(r"\bclass:\s*", attributes)
        if not start:
            continue
        expression = attributes[start.end():]
        next_attribute = re.search(r',\s*(?:[A-Za-z_]\w*|"[^"]*")\s*:', expression)
        found.append((match.group(1), expression[: next_attribute.start()] if next_attribute else expression))
    return found


def _tags_by_class(script: str) -> dict[str, set[str]]:
    tags: dict[str, set[str]] = {}
    for tag, expression in _class_expressions(script):
        for literal in re.findall(r'"([^"]*)"', expression):
            for name in literal.split():
                tags.setdefault(name, set()).add(tag)
    return tags


# One component drawn with two elements on purpose.
SHARED_ON_PURPOSE = {
    "bar": {"div", "i"},            # a stay in the calendar, and its sample in the legend
    "btn": {"a", "button"},         # a button, and a link that looks like one
    "sheet-body": {"div", "form"},  # the body of a sheet, whether it is a form or not
}
TABLE_PARTS = {"table", "thead", "tbody", "tfoot", "tr", "td", "th"}


def test_the_scan_of_class_names_sees_the_script():
    tags = _tags_by_class(site._read("app.js"))
    assert tags["rows"] == {"table"} and tags["months"] == {"div"} and tags["day"] == {"button"}
    assert {"months", "key", "bar", "sheet-body"} <= _own_layout_rules(site._read("style.css"))


def test_a_class_that_lays_out_its_content_names_one_kind_of_element():
    layout, tags = _own_layout_rules(site._read("style.css")), _tags_by_class(site._read("app.js"))
    for name in sorted(layout):
        used_on = tags.get(name, set())
        assert len(used_on) <= 1 or used_on == SHARED_ON_PURPOSE.get(name), (
            f"class {name!r} has a layout rule of its own and is put on {sorted(used_on)}: use two names"
        )
        assert not used_on & TABLE_PARTS, f"class {name!r} would lay a table out as something else"


def test_tables_take_no_class_from_outside_their_builder():
    # A class handed in as an argument is invisible where the table is built, and to the test above.
    tables = [expression for tag, expression in _class_expressions(site._read("app.js")) if tag == "table"]
    assert len(tables) >= 2
    for expression in tables:
        assert re.fullmatch(r'\s*"[\w -]+"\s*', expression), f"table classes must be a plain literal, not {expression.strip()}"


def test_the_title_is_escaped(built):
    _, envelope, _ = built
    page = render_page(envelope, title='Case <script>alert(1)</script> & "co"')
    assert "<title>Case &lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;co&quot;</title>" in page


def test_data_that_could_close_the_script_element_is_refused(built):
    _, envelope, _ = built
    with pytest.raises(SiteError):
        render_page({**envelope, "note": "</script><script>alert(1)"}, title="x")


def test_a_page_without_envelope_yields_nothing():
    assert extract_envelope("<html></html>") is None
    assert extract_envelope('<script type="application/json" id="casecal-data">{nope</script>') is None
    assert extract_envelope('<script type="application/json" id="casecal-data">[1]</script>') is None


def test_the_written_site_can_be_read_back_and_opened(built, tmp_path):
    snapshot, _, page = built
    index = write_site(tmp_path / "out", page)
    assert index.name == "index.html"
    assert (tmp_path / "out" / "robots.txt").read_text() == "User-agent: *\nDisallow: /\n"
    # What a phone needs to keep the page on its home screen like an app: a name and an icon, nothing private.
    import json as _json
    from PIL import Image as _Image
    declared = _json.loads((tmp_path / "out" / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert declared["display"] == "standalone" and declared["start_url"] == "./" and declared["name"] == "Calendario case"
    for entry in declared["icons"] + [{"src": "apple-touch-icon.png", "sizes": "180x180"}]:
        with _Image.open(tmp_path / "out" / entry["src"]) as drawn:
            assert drawn.format == "PNG" and "x".join(map(str, drawn.size)) == entry["sizes"]
    assert 'rel="manifest" href="manifest.webmanifest"' in page and 'rel="apple-touch-icon"' in page
    assert "manifest-src 'self'" in _csp(page) and "default-src 'none'" in _csp(page)
    # The worker that keeps the page for when there is no connection: a file of the site's own, and
    # every file it keeps is one the site publishes.
    worker = (tmp_path / "out" / "sw.js").read_text(encoding="utf-8")
    assert "worker-src 'self'" in _csp(page) and 'register("sw.js")' in page
    for kept in re.search(r"var FILES = \[(.*?)\];", worker).group(1).replace('"', "").split(", "):
        assert kept == "./" or (tmp_path / "out" / kept).is_file()
    envelope = extract_envelope(index.read_text(encoding="utf-8"))
    family, owner = open_envelope(envelope, passphrase=DEMO_PASSPHRASE)
    assert family == snapshot.family_payload()
    assert owner == snapshot.owner_payload()


def test_fetching_a_published_page(monkeypatch, built):
    _, envelope, page = built

    class Response:
        status_code = 200
        text = page
        encoding = None

    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        return Response()

    monkeypatch.setattr(site.requests, "get", fake_get)
    assert site.fetch_published_envelope("https://example.github.io/repo/") == envelope
    assert seen["url"] == "https://example.github.io/repo/index.html"

    Response.status_code = 404
    assert site.fetch_published_envelope("https://example.github.io/repo") is None

    def offline(url, **kwargs):
        raise site.requests.ConnectionError("no network")

    monkeypatch.setattr(site.requests, "get", offline)
    assert site.fetch_published_envelope("https://example.github.io/repo") is None
