"""Reading the workbook of costs from its private bucket. Nothing here touches the network."""

from datetime import datetime, timedelta, timezone

import pytest
import requests

from casecal import r2
from casecal.r2 import (
    EMPTY_SHA256,
    MAX_BYTES,
    NotFound,
    StorageError,
    authorization,
    canonical,
    canonical_query,
    fetch,
    names,
    place,
    sibling,
)

NOW = datetime(2026, 11, 21, 2, 17, 5, tzinfo=timezone.utc)
ACCOUNT = "0123456789abcdef0123456789abcdef"  # an Account ID: 32 hexadecimal characters
API = f"https://{ACCOUNT}.r2.cloudflarestorage.com"
URL = f"{API}/case-private/Spese case.xlsx"
# A key pair shaped like R2's (32 and 64 hexadecimal characters), and plainly not one.
KEY_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
SECRET = "00112233445566778899aabbccddeeff" * 2


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


def test_the_signature_of_a_listing_is_the_one_amazon_publishes_too():
    # "Example: GET Bucket (List Objects)" in the same reference: GET /?max-keys=2&prefix=J.
    header = authorization(
        "GET",
        "examplebucket.s3.amazonaws.com",
        "/",
        {},
        key_id="AKIAIOSFODNN7EXAMPLE",
        secret="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        when=datetime(2013, 5, 24, tzinfo=timezone.utc),
        query=canonical_query({"prefix": "J", "max-keys": "2"}),
        region="us-east-1",
        service="s3",
    )
    assert header.endswith("Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7")
    # Sorted by name, every name and value encoded: also the slash a folder ends with.
    assert canonical_query({"prefix": "conti 2026/è/", "list-type": "2", "delimiter": "/"}) == (
        "delimiter=%2F&list-type=2&prefix=conti%202026%2F%C3%A8%2F"
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

    assert path_of(f"{API}/b/Analisi (2026) è.xlsx") == "/b/Analisi%20%282026%29%20%C3%A8.xlsx"
    assert path_of(f"{API}/b/Analisi%20%282026%29.xlsx") == "/b/Analisi%20%282026%29.xlsx"  # already encoded
    assert path_of(f"{API}/b/cartella/file_v1.2~bozza.xlsx") == "/b/cartella/file_v1.2~bozza.xlsx"
    # Stray spaces around a pasted secret, a bucket in the European jurisdiction, capitals in the host.
    assert path_of(f"  https://{ACCOUNT.upper()}.eu.r2.cloudflarestorage.com/b/f.xlsx \n") == "/b/f.xlsx"
    # The keys as they may be pasted: with a space or a line break, in quotes, with a character
    # nobody sees that came along from a chat.
    expected = authorization(
        "GET", URL.split("/")[2], "/case-private/Spese%20case.xlsx", {}, key_id=KEY_ID, secret=SECRET, when=NOW
    )
    for key_id, secret in ((f"  {KEY_ID}\n", f" {SECRET} "), (f'"{KEY_ID}"', f"'{SECRET}'"), (f"\u200b{KEY_ID}", f"{SECRET}\ufeff")):
        session = FakeSession()
        fetch(URL, key_id, secret, session=session, now=NOW)
        assert session.calls[0][1]["headers"]["Authorization"] == expected


HOST_ONLY = "l'host non è <account>.r2.cloudflarestorage.com"
NO_FILE = "dopo il nome del bucket manca il nome del file"
NO_BUCKET = "manca il nome del bucket"
NO_ACCOUNT = "davanti a «.r2.cloudflarestorage.com» manca l'Account ID"
ODD_ACCOUNT = "davanti a «.r2.cloudflarestorage.com» non c'è un Account ID: sono 32 caratteri, solo cifre e lettere da a a f"
BLANK = "contiene ancora un segnaposto, come <ACCOUNT_ID>: al suo posto va il valore vero"
A_KEY = "sembra una delle due chiavi, non un indirizzo: le chiavi vanno negli altri due secret"
EXTRAS = "contiene «?» o «#»: niente parametri, e un nome di file senza quei segni"
NO_LOGIN = "non deve contenere credenziali né una porta"


@pytest.mark.parametrize(
    "url, reason",
    [
        # What is most often pasted: the endpoint shown beside the keys, or the bucket's own address.
        (API, NO_BUCKET),
        (f"{API}/", NO_BUCKET),
        (f"https://{ACCOUNT}.eu.r2.cloudflarestorage.com", NO_BUCKET),
        (ACCOUNT, NO_BUCKET),                                                        # the Account ID alone
        (f"{API}/case-private", NO_FILE),
        (f"{API}/case-private/", NO_FILE),
        (f"{API}/case-private/cartella/", NO_FILE),
        (f"https://dash.cloudflare.com/{ACCOUNT}/r2/default/buckets/case-private", NO_FILE),  # the bucket's page
        # The line of the instructions, copied with its blanks still to be filled in.
        ("https://<ACCOUNT_ID>.r2.cloudflarestorage.com/case-private", BLANK),
        ("https://<ACCOUNT_ID>.r2.cloudflarestorage.com/<nome-bucket>/<nome-file>.xlsx", BLANK),
        (f"{API}/<bucket>", BLANK),
        ("<ACCOUNT_ID>", BLANK),
        ("https://ACCOUNT_ID.r2.cloudflarestorage.com/case-private", BLANK),
        ("https://account-id.r2.cloudflarestorage.com/case-private", BLANK),
        ("ACCOUNT ID", BLANK),
        # Something else where the Account ID goes: a name, or the ID with a character lost in copying.
        ("https://acc.r2.cloudflarestorage.com/b/f.xlsx", ODD_ACCOUNT),
        (f"https://{ACCOUNT[:-1]}.r2.cloudflarestorage.com/case-private/spese.xlsx", ODD_ACCOUNT),
        (f"https://{ACCOUNT}0.r2.cloudflarestorage.com/case-private/spese.xlsx", ODD_ACCOUNT),
        (f"https://{ACCOUNT[:-1]}g.r2.cloudflarestorage.com/case-private/spese.xlsx", ODD_ACCOUNT),
        ("https://r2.cloudflarestorage.com/b/f.xlsx", NO_ACCOUNT),
        (f"https://{ACCOUNT}.eu.altro.r2.cloudflarestorage.com/b/f.xlsx", HOST_ONLY),
        (SECRET, A_KEY),                                                             # a key, in the wrong secret
        ("https://pub-0123.r2.dev/spese.xlsx", "è l'indirizzo pubblico r2.dev, serve quello dell'API S3"),
        ("https://dash.cloudflare.com/?to=/:account/r2/overview",
         "è una pagina del pannello di Cloudflare in cui non compare l'Account ID"),
        ("https://dash.cloudflare.com/login", "è una pagina del pannello di Cloudflare in cui non compare l'Account ID"),
        (f"https://dash.cloudflare.com/{ACCOUNT}/r2/overview", "è una pagina del pannello di Cloudflare, ma non quella di un bucket"),
        (f"https://dash.cloudflare.com/{ACCOUNT}/home", "è una pagina del pannello di Cloudflare, ma non quella di un bucket"),
        ("https://developers.cloudflare.com/r2/", "è una pagina di Cloudflare, non l'indirizzo dell'API S3"),
        (f"http://{ACCOUNT}.r2.cloudflarestorage.com/b/f.xlsx", "deve iniziare con https://"),  # not encrypted in transit
        ("https://example.com/b/f.xlsx", HOST_ONLY),
        (f"https://{ACCOUNT}.r2.cloudflarestorage.com.example.com/b/f.xlsx", HOST_ONLY),
        ("case-private/spese.xlsx", HOST_ONLY),                                      # a bucket and a file, nowhere
        ("", HOST_ONLY),
        (f"{API}/b/f.xlsx?X-Amz-Signature=abc", EXTRAS),                             # a presigned link
        (f"{API}/b/f.xlsx#foglio", EXTRAS),
        (f"https://user:pass@{ACCOUNT}.r2.cloudflarestorage.com/b/f.xlsx", NO_LOGIN),
        (f"{API}:8443/b/f.xlsx", NO_LOGIN),
        (f"{API}:porta/b/f.xlsx", "non è un indirizzo"),
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
    assert message == f"COSTS_R2_URL non valido: {reason}. {r2.EXPECTED}"
    for given in (ACCOUNT, ACCOUNT[:-1], "case-private", "spese", "pub-0123", "example.com", "user", "pass", "Signature", SECRET):
        assert given not in message


def test_what_the_owner_has_at_hand_is_enough_to_say_where_the_bucket_is():
    plain = f"{API}/case-private"
    european = f"https://{ACCOUNT}.eu.r2.cloudflarestorage.com/case-private"
    # The Account ID alone, or the endpoint Cloudflare shows beside the keys: the configuration names the bucket.
    for pasted in (ACCOUNT, ACCOUNT.upper(), f" {ACCOUNT}\n", f'"{ACCOUNT}"', f"“{ACCOUNT}”", f"\u200b{ACCOUNT}\ufeff", API, f"{API}/"):
        assert canonical(pasted, "case-private") == plain
    assert canonical(f"https://{ACCOUNT}.eu.r2.cloudflarestorage.com", "case-private") == european
    # A bucket the address names itself is the one meant, whatever the configuration says.
    assert canonical(plain, "un-altro") == plain and canonical(f"{ACCOUNT}/case-private", "un-altro") == plain
    assert canonical(f"{plain}/conti/Spese case.xlsx", "un-altro") == f"{plain}/conti/Spese%20case.xlsx"
    assert canonical(f"{plain}/conti/", None) == f"{plain}/conti/"
    # The page of the bucket in Cloudflare's dashboard, as the browser's address bar has it:
    # account, jurisdiction ("default" for none) and bucket are all there; the tab shown is not a file.
    page = f"https://dash.cloudflare.com/{ACCOUNT}/r2/default/buckets/case-private"
    for pasted in (page, f"{page}/", f"{page}/settings", f"{page}/objects?prefix=conti%2F", f"{page}#top", page.replace(ACCOUNT, ACCOUNT.upper())):
        assert canonical(pasted) == plain and canonical(pasted, "un-altro") == plain
    assert canonical(f"https://dash.cloudflare.com/{ACCOUNT}/r2/eu/buckets/case-private/settings") == european
    assert canonical(f"https://dash.cloudflare.com/{ACCOUNT}/r2/buckets/case-private") == plain  # as it once was
    # Any other page of the dashboard still says which account it is.
    for pasted in (f"https://dash.cloudflare.com/{ACCOUNT}/r2/overview", f"https://dash.cloudflare.com/{ACCOUNT}", f"dash.cloudflare.com/{ACCOUNT}/home"):
        assert canonical(pasted, "case-private") == plain
    # What comes out is an address every other function here takes as it is.
    where = place(canonical(ACCOUNT, "case-private"))
    assert (where.host, where.bucket, where.key) == (f"{ACCOUNT}.r2.cloudflarestorage.com", "case-private", "")
    assert sibling(canonical(page), "santa.jpg") == f"{plain}/santa.jpg"


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
    assert target(f"“{plain}”") == plain and target(f"`{plain}`") == plain and target(f"« {plain} »") == plain
    assert target(f"  {plain}\n") == plain and target(f"\ufeff{plain}\u200b") == plain
    # With the Account ID alone in place of the host.
    assert target(f"{ACCOUNT}/case-private/spese.xlsx") == plain
    # With the bucket named before the host: the same place, said the other way.
    assert target(f"https://case-private.{ACCOUNT}.r2.cloudflarestorage.com/spese.xlsx") == plain
    assert target(f"https://bucket-account.{ACCOUNT}.r2.cloudflarestorage.com/nome.xlsx") == (
        f"https://{ACCOUNT}.r2.cloudflarestorage.com/bucket-account/nome.xlsx"
    )  # words of the instructions, here the bucket's and the file's own names
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


def answer(status, code=None):
    """What R2 sends back when it refuses: a status and, in the body, its own word for the refusal."""
    body = b"<html>errore</html>" if code is None else (
        f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code>'
        "<Message>The bucket case-private of account 0123456789abcdef does not hold Spese case.xlsx</Message>"
        "<Key>Spese case.xlsx</Key><BucketName>case-private</BucketName></Error>"
    ).encode("utf-8")
    return FakeResponse(status, chunks=(body,))


DENIED = "accesso negato: la chiave non può leggere questo bucket"
UNKNOWN_KEY = "Access Key ID non riconosciuto da Cloudflare: incollato male, oppure token eliminato"
UNKNOWN_KEYS = "chiavi non riconosciute da Cloudflare: incollate male, oppure token eliminato o scaduto"
WRONG_SECRET = "firma rifiutata: la Secret Access Key non è quella di questo Access Key ID"
REFUSED = "chiave rifiutata, oppure senza il permesso di lettura su questo bucket"
NO_SUCH_BUCKET = "bucket non trovato: controlla il nome del bucket (per un bucket con giurisdizione europea l'host contiene «.eu.»)"
NO_SUCH_FILE = "file non trovato: controlla il nome del bucket e del file in COSTS_R2_URL"


@pytest.mark.parametrize(
    "status, code, words",
    [
        # What R2 calls the refusal tells a wrong key from a key that may not read this bucket.
        (403, "AccessDenied", DENIED),
        (403, "SignatureDoesNotMatch", WRONG_SECRET),
        (403, "InvalidAccessKeyId", UNKNOWN_KEY),
        (401, "Unauthorized", UNKNOWN_KEYS),
        (403, "ExpiredToken", "chiave scaduta: va creato un nuovo token"),
        (403, "UnaParolaMaiVista", REFUSED),
        (403, None, REFUSED),
        (401, None, REFUSED),
        (404, "NoSuchKey", NO_SUCH_FILE),
        (404, None, NO_SUCH_FILE),
        (404, "NoSuchBucket", NO_SUCH_BUCKET),
        (301, "PermanentRedirect", "HTTP 301 (PermanentRedirect)"),
        (400, "InvalidArgument", "HTTP 400 (InvalidArgument)"),
        (503, "SlowDown", "HTTP 503 (SlowDown)"),
        (500, "UnaParolaMaiVista", "HTTP 500"),
        (500, None, "HTTP 500"),
        (206, None, "HTTP 206"),
    ],
)
def test_a_refusal_is_explained_without_naming_bucket_file_or_key(status, code, words):
    session = FakeSession(answer(status, code))
    with pytest.raises(StorageError) as caught:
        fetch(URL, KEY_ID, SECRET, session=session, now=NOW)
    message = str(caught.value)
    assert message == words or message.startswith(words + " (")
    # Of R2's answer only its word for the refusal is ever repeated, and only one of the protocol's own.
    for private in ("0123456789abcdef", "case-private", "Spese", KEY_ID, SECRET, "UnaParolaMaiVista", "Message", "does not hold"):
        assert private not in message
    assert session.response.closed


def test_a_refusal_adds_what_looks_wrong_in_the_keys_by_their_shape_alone():
    def refusal(key_id, secret, code="SignatureDoesNotMatch"):
        with pytest.raises(StorageError) as caught:
            fetch(URL, key_id, secret, session=FakeSession(answer(403, code)), now=NOW)
        message = str(caught.value)
        assert key_id.strip() not in message and secret.strip() not in message
        return message

    id_hint = "COSTS_R2_ACCESS_KEY_ID non ha la forma di un Access Key ID di R2 (32 caratteri esadecimali)"
    secret_hint = (
        "COSTS_R2_SECRET_ACCESS_KEY non ha la forma di una Secret Access Key di R2 "
        "(64 caratteri esadecimali): non è il «Token value»"
    )
    # A pair of the right shape: nothing to add, whatever the refusal.
    assert refusal(KEY_ID, SECRET) == WRONG_SECRET
    assert refusal(f" {KEY_ID.upper()}\n", f"'{SECRET}'") == WRONG_SECRET
    # The token's value pasted as the secret, as Cloudflare shows it first; a key cut short.
    token_value = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AB"
    assert refusal(KEY_ID, token_value) == f"{WRONG_SECRET}. Nota: {secret_hint}"
    assert refusal(KEY_ID[:-2], SECRET, "InvalidAccessKeyId") == f"{UNKNOWN_KEY}. Nota: {id_hint}"
    assert refusal("la-mia-chiave", "il-mio-segreto", None) == f"{REFUSED}. Nota: {id_hint}; {secret_hint}"
    # Each in the other's secret.
    assert refusal(SECRET, KEY_ID) == f"{WRONG_SECRET}. Nota: le due chiavi sembrano scambiate tra i due secret"
    # Only a refusal brings the note: a pair of another shape that R2 accepts is none of this module's business.
    assert fetch(URL, "la-mia-chiave", "il-mio-segreto", session=FakeSession(), now=NOW) == b"PK\x03\x04..."


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
    with pytest.raises(NotFound):
        fetch(URL, KEY_ID, SECRET, session=FakeSession(answer(404, "NoSuchKey")), now=NOW)
    assert issubclass(NotFound, StorageError)
    # A bucket that is not there is no missing photo: nothing could be found in it.
    with pytest.raises(StorageError) as caught:
        fetch(URL, KEY_ID, SECRET, session=FakeSession(answer(404, "NoSuchBucket")), now=NOW)
    assert not isinstance(caught.value, NotFound) and "bucket non trovato" in str(caught.value)
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


def test_an_address_points_to_a_bucket_a_folder_or_a_file():
    base = f"https://{ACCOUNT}.r2.cloudflarestorage.com"
    for url, key, is_file, folder in (
        (f"{base}/case-private", "", False, ""),
        (f"{base}/case-private/", "", False, ""),
        (f"{base}/case-private/conti/", "conti/", False, "conti/"),
        (f"{base}/case-private/Spese case.xlsx", "Spese case.xlsx", True, ""),
        (f"{base}/case-private/conti/2026/spese.xlsx", "conti/2026/spese.xlsx", True, "conti/2026/"),
        (f"{base}/case-private/Spese%20case.xlsx", "Spese case.xlsx", True, ""),  # already encoded
    ):
        where = place(url)
        assert (where.host, where.bucket, where.key) == (f"{ACCOUNT}.r2.cloudflarestorage.com", "case-private", key)
        assert (where.names_a_file, where.folder) == (is_file, folder)
    with pytest.raises(StorageError, match="manca il nome del bucket"):
        place(base)
    assert place(base, "case-private") == place(f"{base}/case-private")


def test_a_file_kept_beside_another_has_the_same_bucket_and_folder():
    base = API
    assert sibling(f"{base}/case-private/Spese case.xlsx", "santa.jpg") == f"{base}/case-private/santa.jpg"
    assert sibling(f"{base}/b/conti/2026/spese.xlsx", "vassena.jpg") == f"{base}/b/conti/2026/vassena.jpg"
    assert sibling(f"{base}/b/Cartella con spazi/spese.xlsx", "santa.jpg") == f"{base}/b/Cartella%20con%20spazi/santa.jpg"
    # An address of the bucket, or of a folder, is where the file goes.
    assert sibling(f"{base}/case-private", "santa.jpg") == f"{base}/case-private/santa.jpg"
    assert sibling(f"{base}/case-private/", "Spese case.xlsx") == f"{base}/case-private/Spese%20case.xlsx"
    assert sibling(f"{base}/case-private/conti/", "santa.jpg") == f"{base}/case-private/conti/santa.jpg"
    # The name is one file's, whatever it contains: it cannot step out of the folder.
    assert sibling(f"{base}/b/conti/spese.xlsx", "../altro/x.jpg") == f"{base}/b/conti/..%2Faltro%2Fx.jpg"
    assert sibling(f" https://{ACCOUNT.upper()}.eu.r2.cloudflarestorage.com/b/spese.xlsx ", "casa mia.jpg") == (
        f"https://{ACCOUNT}.eu.r2.cloudflarestorage.com/b/casa%20mia.jpg"
    )
    # What it makes is an address the fetch accepts.
    session = FakeSession()
    fetch(sibling(URL, "santa.jpg"), KEY_ID, SECRET, session=session, now=NOW)
    assert session.calls[0][0].endswith("/case-private/santa.jpg")
    with pytest.raises(StorageError, match="COSTS_R2_URL non valido"):
        sibling("https://example.com/b/spese.xlsx", "santa.jpg")


# --------------------------------------------------------------------------- what is in a bucket

BUCKET = f"https://{ACCOUNT}.r2.cloudflarestorage.com/case-private"


def listed(*keys, truncated=False, prefix=""):
    """What R2 answers when asked what a bucket holds, as S3 words it."""
    contents = "".join(
        f"<Contents><Key>{key}</Key><LastModified>2026-11-22T08:00:00.000Z</LastModified>"
        f"<ETag>&quot;0123&quot;</ETag><Size>14559</Size><StorageClass>STANDARD</StorageClass></Contents>"
        for key in keys
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<Name>case-private</Name><Prefix>{prefix}</Prefix><Delimiter>/</Delimiter><MaxKeys>1000</MaxKeys>"
        f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>{contents}"
        "<CommonPrefixes><Prefix>vecchi/</Prefix></CommonPrefixes><KeyCount>3</KeyCount>"
        "</ListBucketResult>"
    ).encode("utf-8")


def test_the_files_of_a_bucket_are_asked_for_with_a_signed_get():
    session = FakeSession(FakeResponse(chunks=(listed("Analisi Entrate-Spese.xlsx", "IMG_0001.jpeg", "foto &amp; altro.png"),)))
    assert names(BUCKET, KEY_ID, SECRET, session=session, now=NOW) == ["Analisi Entrate-Spese.xlsx", "IMG_0001.jpeg", "foto & altro.png"]
    (url, settings), = session.calls
    host = f"{ACCOUNT}.r2.cloudflarestorage.com"
    query = "delimiter=%2F&list-type=2&max-keys=1000"
    assert url == f"https://{host}/case-private?{query}"  # the query as it is signed
    assert settings["allow_redirects"] is False
    assert settings["headers"]["Authorization"] == authorization(
        "GET", host, "/case-private", {}, key_id=KEY_ID, secret=SECRET, when=NOW, query=query
    )
    assert session.response.closed


def test_the_files_listed_are_those_of_the_folder_pointed_at_and_no_deeper():
    def asked(url, *keys):
        session = FakeSession(FakeResponse(chunks=(listed(*keys),)))
        found = names(url, KEY_ID, SECRET, session=session, now=NOW)
        return session.calls[0][0].split("?", 1)[1], found

    # A folder: only what is directly in it, named without the folder.
    query, found = asked(f"{BUCKET}/conti 2026/", "conti 2026/", "conti 2026/spese.xlsx", "conti 2026/santa.jpg")
    assert query == "delimiter=%2F&list-type=2&max-keys=1000&prefix=conti%202026%2F"
    assert found == ["spese.xlsx", "santa.jpg"]  # not the folder's own placeholder
    # The address of a file: the folder that file is in.
    query, found = asked(f"{BUCKET}/conti/spese.xlsx", "conti/spese.xlsx", "conti/vassena.jpg")
    assert query.endswith("&prefix=conti%2F") and found == ["spese.xlsx", "vassena.jpg"]
    query, found = asked(f"{BUCKET}/spese.xlsx", "spese.xlsx")
    assert "prefix" not in query and found == ["spese.xlsx"]
    # An empty bucket.
    assert asked(BUCKET)[1] == []


def test_a_listing_that_cannot_be_trusted_is_refused_in_words_that_name_nothing():
    def refusal(response):
        with pytest.raises(StorageError) as caught:
            names(BUCKET, KEY_ID, SECRET, session=FakeSession(response), now=NOW)
        message = str(caught.value)
        for private in (ACCOUNT, "case-private", KEY_ID, SECRET, "Analisi"):
            assert private not in message
        return caught.value

    assert "troppi file" in str(refusal(FakeResponse(chunks=(listed("Analisi.xlsx", truncated=True),))))
    for junk in (b"", b"non sono un elenco", b"<html><body>errore</body></html>", b"<ListBucketResult><Contents>"):
        assert str(refusal(FakeResponse(chunks=(junk,)))) == "risposta di Cloudflare R2 non riconosciuta"
    # Nothing that would make the reader expand or fetch anything.
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><ListBucketResult><Contents><Key>&a;</Key></Contents></ListBucketResult>'
    assert str(refusal(FakeResponse(chunks=(bomb,)))) == "risposta di Cloudflare R2 non riconosciuta"
    assert str(refusal(FakeResponse(403))) == REFUSED
    assert str(refusal(answer(403, "AccessDenied"))).startswith(DENIED)
    assert str(refusal(FakeResponse(500))) == "HTTP 500"
    for missing in (refusal(FakeResponse(404)), refusal(answer(404, "NoSuchBucket"))):
        assert str(missing) == NO_SUCH_BUCKET
    endless = FakeResponse(chunks=(b"x" * (1024 * 1024),) * 3)
    assert "troppo grande" in str(refusal(endless))
