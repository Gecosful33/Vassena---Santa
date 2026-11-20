import base64
import hashlib
import html
import re
from datetime import datetime, timezone

import pytest

from casecal import site
from casecal.crypto import build_envelope, open_envelope, site_salt
from casecal.demo import DEMO_FAMILY_PASSPHRASE, DEMO_OWNER_PASSPHRASE, demo_snapshot
from casecal.site import SiteError, extract_envelope, render_page, write_site

NOW = datetime(2026, 8, 15, 2, 17, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def built():
    snapshot = demo_snapshot(NOW)
    envelope = build_envelope(
        family_passphrase=DEMO_FAMILY_PASSPHRASE,
        owner_passphrase=DEMO_OWNER_PASSPHRASE,
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
    assert DEMO_FAMILY_PASSPHRASE not in page
    assert DEMO_OWNER_PASSPHRASE not in page


def test_the_csp_pins_exactly_the_inlined_code(built):
    _, _, page = built
    csp = _csp(page)
    script = re.search(r"<script>(.*?)</script>", page, re.DOTALL).group(1)
    style = re.search(r"<style>(.*?)</style>", page, re.DOTALL).group(1)
    assert f"script-src '{_sha256(script)}'" in csp
    assert f"style-src '{_sha256(style)}'" in csp
    assert "default-src 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    # default-src 'none' with no connect-src: the page cannot send anything anywhere.
    assert "connect-src" not in csp


def test_the_page_loads_nothing_from_the_network(built):
    _, _, page = built
    assert not re.search(r"""(?:src|href|action)\s*=\s*["']?(?:https?:)?//""", page)
    assert "@import" not in page
    assert "noindex" in page and 'name="referrer" content="no-referrer"' in page


def test_the_code_never_parses_strings_as_markup():
    script = "".join(site._read(name) for name in site.SCRIPTS)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script


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
    envelope = extract_envelope(index.read_text(encoding="utf-8"))
    family, owner = open_envelope(envelope, owner_passphrase=DEMO_OWNER_PASSPHRASE)
    assert family == snapshot.family_payload()
    assert owner["properties"] == snapshot.owner_payload()["properties"]


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
