"""Reading the workbook of costs from its private bucket. Nothing here touches the network."""

from datetime import datetime, timedelta, timezone

import pytest
import requests

from casecal import r2
from casecal.r2 import EMPTY_SHA256, MAX_BYTES, NotFound, StorageError, authorization, fetch, sibling

NOW = datetime(2026, 11, 21, 2, 17, 5, tzinfo=timezone.utc)
URL = "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/case-private/Spese case.xlsx"
KEY_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
SECRET = "a-secret-that-is-not-one-0123456789abcdef0123456789abcdef"


def test_the_signature_is_the_one_amazon_publishes_for_its_worked_example():
    # "Example: GET Object" in the Amazon S3 API Reference, "Signature Calculations for
    # the Authorization Header". The key pair is the documentation's own, not a real one.
    header = authorization(
        "GET",
        "examplebucket.s3.amazonaws.com",
        "/test.txt",
        {"Range": "bytes=0-9"},
        key_id="AKIAIOSFODNN7EXAMPLE",
        secret="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        when=datetime(2013, 5, 24, tzinfo=timezone.utc),
        region="us-east-1",
        service="s3",
    )
    assert header == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_the_signature_follows_the_moment_whatever_its_time_zone():
    utc = authorization("GET", "h.r2.cloudflarestorage.com", "/b/k", {}, key_id="id", secret="s", when=NOW)
    rome = authorization(
        "GET", "h.r2.cloudflarestorage.com", "/b/k", {}, key_id="id", secret="s",
        when=NOW.astimezone(timezone(timedelta(hours=1))),
    )
    assert utc == rome
    later = authorization(
        "GET", "h.r2.cloudflarestorage.com", "/b/k", {}, key_id="id", secret="s", when=NOW.replace(second=6)
    )
    assert later != utc
    # R2 has a single region, "auto".
    assert "Credential=id/20261121/auto/s3/aws4_request" in utc


class FakeResponse:
    def __init__(self, status=200, chunks=(b"PK", b"\x03\x04", b"...")):
        self.status_code, self.chunks, self.closed = status, chunks, False

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response or FakeResponse(), error, []

    def get(self, url, **settings):
        self.calls.append((url, settings))
        if self.error:
            raise self.error
        return self.response


def test_the_file_is_fetched_with_a_signed_get_and_nothing_else():
    session = FakeSession()
    assert fetch(URL, KEY_ID, SECRET, session=session, now=NOW) == b"PK\x03\x04..."
    assert session.response.closed
    (url, settings), = session.calls
    host = "0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"
    assert url == f"https://{host}/case-private/Spese%20case.xlsx"  # the path as it is signed
    assert settings["allow_redirects"] is False and settings["stream"] is True and settings["timeout"] > 0
    headers = settings["headers"]
    assert headers["x-amz-date"] == "20261121T021705Z"
    assert headers["x-amz-content-sha256"] == EMPTY_SHA256
    assert headers["Authorization"] == authorization(
        "GET", host, "/case-private/Spese%20case.xlsx", {}, key_id=KEY_ID, secret=SECRET, when=NOW
    )
    assert headers["Authorization"].startswith(
        f"AWS4-HMAC-SHA256 Credential={KEY_ID}/20261121/auto/s3/aws4_request, "
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="
    )
    assert SECRET not in headers["Authorization"]
    assert set(headers) == {"Authorization", "x-amz-date", "x-amz-content-sha256"}


def test_a_name_is_signed_the_way_it_is_sent():
    def path_of(url):
        session = FakeSession()
        fetch(url, KEY_ID, SECRET, session=session, now=NOW)
        return session.calls[0][0].split(".com", 1)[1]

    base = "https://acc.r2.cloudflarestorage.com"
    assert path_of(f"{base}/b/Analisi (2026) è.xlsx") == "/b/Analisi%20%282026%29%20%C3%A8.xlsx"
    assert path_of(f"{base}/b/Analisi%20%282026%29.xlsx") == "/b/Analisi%20%282026%29.xlsx"  # already encoded
    assert path_of(f"{base}/b/cartella/file_v1.2~bozza.xlsx") == "/b/cartella/file_v1.2~bozza.xlsx"
    # Stray spaces around a pasted secret, a bucket in the European jurisdiction, capitals in the host.
    assert path_of("  https://ACC.eu.r2.cloudflarestorage.com/b/f.xlsx \n") == "/b/f.xlsx"
    session = FakeSession()
    fetch(URL, f"  {KEY_ID}\n", f" {SECRET} ", session=session, now=NOW)
    assert session.calls[0][1]["headers"]["Authorization"] == authorization(
        "GET", URL.split("/")[2], "/case-private/Spese%20case.xlsx", {}, key_id=KEY_ID, secret=SECRET, when=NOW
    )


ACCOUNT = "0123456789abcdef0123456789abcdef"  # an Account ID: 32 hexadecimal characters
HOST_ONLY = "l'host non è <account>.r2.cloudflarestorage.com"
NO_FILE = "dopo il nome del bucket manca il nome del file"
NO_BUCKET = "dopo l'host mancano il nome del bucket e il nome del file"
EXTRAS = "contiene «?» o «#»: niente parametri, e un nome di file senza quei segni"
NO_LOGIN = "non deve contenere credenziali né una porta"


@pytest.mark.parametrize(
    "url, reason",
    [
        # What is most often pasted: the endpoint shown beside the keys, or the bucket's own address.
        (f"https://{ACCOUNT}.r2.cloudflarestorage.com", NO_BUCKET),
        (f"https://{ACCOUNT}.r2.cloudflarestorage.com/", NO_BUCKET),
        (f"https://{ACCOUNT}.eu.r2.cloudflarestorage.com", NO_BUCKET),
        (f"https://{ACCOUNT}.r2.cloudflarestorage.com/case-private", NO_FILE),
        (f"https://{ACCOUNT}.r2.cloudflarestorage.com/case-private/", NO_FILE),
        (f"https://{ACCOUNT}.r2.cloudflarestorage.com/case-private/cartella/", NO_FILE),
        ("https://pub-0123.r2.dev/spese.xlsx", "è l'indirizzo pubblico r2.dev, serve quello dell'API S3"),
        (f"https://dash.cloudflare.com/{ACCOUNT}/r2/default/buckets/case-private",
         "è una pagina del pannello di Cloudflare, serve l'indirizzo dell'API S3"),
        ("http://acc.r2.cloudflarestorage.com/b/f.xlsx", "deve iniziare con https://"),  # not encrypted in transit
        ("https://example.com/b/f.xlsx", HOST_ONLY),
        ("https://r2.cloudflarestorage.com/b/f.xlsx", HOST_ONLY),                    # no account
        ("https://acc.r2.cloudflarestorage.com.example.com/b/f.xlsx", HOST_ONLY),
        ("case-private/spese.xlsx", HOST_ONLY),                                      # a bucket and a file, nowhere
        ("", HOST_ONLY),
        ("https://acc.r2.cloudflarestorage.com/b/f.xlsx?X-Amz-Signature=abc", EXTRAS),  # a presigned link
        ("https://acc.r2.cloudflarestorage.com/b/f.xlsx#foglio", EXTRAS),
        ("https://user:pass@acc.r2.cloudflarestorage.com/b/f.xlsx", NO_LOGIN),
        ("https://acc.r2.cloudflarestorage.com:8443/b/f.xlsx", NO_LOGIN),
        ("https://acc.r2.cloudflarestorage.com:porta/b/f.xlsx", "non è un indirizzo"),
    ],
)
def test_an_address_that_is_not_a_file_in_a_bucket_is_refused_before_any_request(url, reason):
    session = FakeSession()
    with pytest.raises(StorageError) as caught:
        fetch(url, KEY_ID, SECRET, session=session, now=NOW)
    assert session.calls == []
    # The reason says which part is wrong and shows the shape wanted, in words: the log is public,
    # and the address names the account, the bucket and the file.
    message = str(caught.value)
    assert message == f"COSTS_R2_URL non valido: {reason}. Forma attesa: {r2.URL_SHAPE}"
    for given in (ACCOUNT, "case-private", "spese", "pub-0123", "example.com", "user", "pass", "Signature"):
        assert given not in message


def test_an_address_pasted_a_little_differently_is_still_understood():
    def target(url):
        session = FakeSession()
        fetch(url, KEY_ID, SECRET, session=session, now=NOW)
        return session.calls[0][0]

    plain = f"https://{ACCOUNT}.r2.cloudflarestorage.com/case-private/spese.xlsx"
    assert target(plain) == plain
    # Without its scheme, or with the quotes or brackets it was shown in.
    assert target(f"{ACCOUNT}.r2.cloudflarestorage.com/case-private/spese.xlsx") == plain
    assert target(f'"{plain}"') == plain and target(f"'{plain}'") == plain and target(f"<{plain}>") == plain
    assert target(f"  {plain}\n") == plain
    # With the bucket named before the host: the same place, said the other way.
    assert target(f"https://case-private.{ACCOUNT}.r2.cloudflarestorage.com/spese.xlsx") == plain
    assert target(f"https://case-private.{ACCOUNT}.eu.r2.cloudflarestorage.com/conti/spese.xlsx") == (
        f"https://{ACCOUNT}.eu.r2.cloudflarestorage.com/case-private/conti/spese.xlsx"
    )
    # A bucket in the European jurisdiction is not a bucket named "eu"... nor is the account one.
    assert target(f"https://{ACCOUNT}.eu.r2.cloudflarestorage.com/case-private/spese.xlsx") == (
        f"https://{ACCOUNT}.eu.r2.cloudflarestorage.com/case-private/spese.xlsx"
    )
    # The request is signed for the host it is sent to.
    session = FakeSession()
    fetch(f"https://case-private.{ACCOUNT}.r2.cloudflarestorage.com/spese.xlsx", KEY_ID, SECRET, session=session, now=NOW)
    assert session.calls[0][1]["headers"]["Authorization"] == authorization(
        "GET", f"{ACCOUNT}.r2.cloudflarestorage.com", "/case-private/spese.xlsx", {}, key_id=KEY_ID, secret=SECRET, when=NOW
    )


@pytest.mark.parametrize(
    "status, words",
    [
        (401, "chiave rifiutata"),
        (403, "chiave rifiutata"),
        (404, "file non trovato"),
        (301, "HTTP 301"),
        (500, "HTTP 500"),
        (206, "HTTP 206"),
    ],
)
def test_a_refusal_is_explained_without_naming_bucket_file_or_key(status, words):
    session = FakeSession(FakeResponse(status, chunks=(b"<Error><Code>AccessDenied</Code><Key>Spese case.xlsx</Key></Error>",)))
    with pytest.raises(StorageError) as caught:
        fetch(URL, KEY_ID, SECRET, session=session, now=NOW)
    message = str(caught.value)
    assert words in message
    for private in ("0123456789abcdef", "case-private", "Spese", KEY_ID, SECRET, "AccessDenied"):
        assert private not in message
    assert session.response.closed


def test_a_network_failure_is_reported_by_its_kind_only():
    broken = requests.ConnectionError(f"HTTPSConnectionPool(host='{URL}'): Max retries exceeded")
    for session in (FakeSession(error=broken), FakeSession(FakeResponse(chunks=(b"PK", broken)))):
        with pytest.raises(StorageError) as caught:
            fetch(URL, KEY_ID, SECRET, session=session, now=NOW)
        assert str(caught.value) == "Cloudflare R2 non raggiungibile (ConnectionError)"
        assert caught.value.__cause__ is None and caught.value.__suppress_context__  # no address in a traceback
    with pytest.raises(StorageError, match=r"non raggiungibile \(ReadTimeout\)"):
        fetch(URL, KEY_ID, SECRET, session=FakeSession(error=requests.ReadTimeout("slow")), now=NOW)


def test_a_file_too_large_to_be_the_workbook_is_not_downloaded_whole():
    megabyte = b"x" * (1024 * 1024)
    served = []

    class Endless(FakeResponse):
        def iter_content(self, chunk_size):
            while True:
                served.append(1)
                yield megabyte

    with pytest.raises(StorageError, match="file troppo grande"):
        fetch(URL, KEY_ID, SECRET, session=FakeSession(Endless()), now=NOW)
    assert len(served) == MAX_BYTES // len(megabyte) + 1  # stopped as soon as the limit was passed
    exact = FakeSession(FakeResponse(chunks=(megabyte,) * (MAX_BYTES // len(megabyte))))
    assert len(fetch(URL, KEY_ID, SECRET, session=exact, now=NOW)) == MAX_BYTES


def test_a_missing_file_is_told_apart_from_a_refusal():
    # A house without a photo is not a failure: the caller has to know which of the two it is.
    with pytest.raises(NotFound):
        fetch(URL, KEY_ID, SECRET, session=FakeSession(FakeResponse(404)), now=NOW)
    assert issubclass(NotFound, StorageError)
    for status in (401, 403, 500):
        with pytest.raises(StorageError) as caught:
            fetch(URL, KEY_ID, SECRET, session=FakeSession(FakeResponse(status)), now=NOW)
        assert not isinstance(caught.value, NotFound)


def test_the_size_a_file_may_have_is_the_callers_to_say():
    megabyte = b"x" * (1024 * 1024)
    seven = FakeResponse(chunks=(megabyte,) * 7)
    with pytest.raises(StorageError, match=r"file troppo grande \(oltre 5 MB\)"):
        fetch(URL, KEY_ID, SECRET, session=FakeSession(seven), now=NOW)
    assert len(fetch(URL, KEY_ID, SECRET, session=FakeSession(seven), now=NOW, max_bytes=8 * len(megabyte))) == 7 * len(megabyte)
    with pytest.raises(StorageError, match=r"file troppo grande \(oltre 2 MB\)"):
        fetch(URL, KEY_ID, SECRET, session=FakeSession(seven), now=NOW, max_bytes=2 * len(megabyte))


def test_a_file_kept_beside_another_has_the_same_bucket_and_folder():
    base = "https://acc.r2.cloudflarestorage.com"
    assert sibling(f"{base}/case-private/Spese case.xlsx", "santa.jpg") == f"{base}/case-private/santa.jpg"
    assert sibling(f"{base}/b/conti/2026/spese.xlsx", "vassena.jpg") == f"{base}/b/conti/2026/vassena.jpg"
    assert sibling(f"{base}/b/Cartella con spazi/spese.xlsx", "santa.jpg") == f"{base}/b/Cartella%20con%20spazi/santa.jpg"
    # The name is one file's, whatever it contains: it cannot step out of the folder.
    assert sibling(f"{base}/b/conti/spese.xlsx", "../altro/x.jpg") == f"{base}/b/conti/..%2Faltro%2Fx.jpg"
    assert sibling(f" https://ACC.eu.r2.cloudflarestorage.com/b/spese.xlsx ", "casa mia.jpg") == "https://acc.eu.r2.cloudflarestorage.com/b/casa%20mia.jpg"
    # What it makes is an address the fetch accepts.
    session = FakeSession()
    fetch(sibling(URL, "santa.jpg"), KEY_ID, SECRET, session=session, now=NOW)
    assert session.calls[0][0].endswith("/case-private/santa.jpg")
    with pytest.raises(StorageError, match="COSTS_R2_URL non valido"):
        sibling("https://example.com/b/spese.xlsx", "santa.jpg")
