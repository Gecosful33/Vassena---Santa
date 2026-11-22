"""Read from a private Cloudflare R2 bucket: a file, or the names of the files in a folder.

R2 speaks the S3 protocol: GET requests signed with AWS Signature Version 4, with
a key the owner creates for this purpose alone (permission "Object Read only",
limited to the bucket). Nothing is ever written there.

The signing is done here, in a few lines, rather than through an SDK: two kinds
of request do not justify a dependency of that size in an unattended job. It is
held to the worked examples Amazon publishes (see the tests).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import quote, unquote, urlsplit

import requests

REGION, SERVICE = "auto", "s3"  # R2 has one region, called "auto"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_BYTES = 5 * 1024 * 1024
LISTING_MAX_BYTES = 2 * 1024 * 1024
LISTING_MAX_NAMES = 1000  # one page of a listing: a bucket of the owner's few files is far below it
TIMEOUT_SECONDS = 30

_ENDPOINT = ("r2", "cloudflarestorage", "com")  # what every address of the S3 API ends with
_ACCOUNT_RE = re.compile(r"^[0-9a-f]{32}$")  # what Cloudflare calls the Account ID
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_KEY_LIKE_RE = re.compile(r"^[0-9a-f]{64}$")  # a Secret Access Key, pasted where the address goes
# A value copied from the instructions with a blank still to be filled in: "<ACCOUNT_ID>" left
# as it was written, or the same words where the account goes.
_BLANK_RE = re.compile(r"[<>]")
_BLANK_HOST_RE = re.compile(r"account|bucket|nome", re.IGNORECASE)
# Characters that take no room on a screen and come along when a value is copied from a chat.
_UNSEEN_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
# What the address must look like, as its owner finds it: Cloudflare shows the Account ID and
# the address of the S3 API, each ready to be copied.
EXPECTED = (
    "Forma attesa: l'indirizzo «S3 API» del bucket, "
    "https://<account>.r2.cloudflarestorage.com/<bucket>, seguito se serve da /<file>; "
    "se properties.toml nomina il bucket basta l'Account ID di Cloudflare (32 caratteri)"
)
_NO_FILE = "dopo il nome del bucket manca il nome del file"
_NO_BUCKET = "manca il nome del bucket"
_NOT_THE_API = "l'host non è <account>.r2.cloudflarestorage.com"
_PLACEHOLDER = "contiene ancora un segnaposto, come <ACCOUNT_ID>: al suo posto va il valore vero"
_NO_ACCOUNT = "davanti a «.r2.cloudflarestorage.com» manca l'Account ID"
_ODD_ACCOUNT = (
    "davanti a «.r2.cloudflarestorage.com» non c'è un Account ID: "
    "sono 32 caratteri, solo cifre e lettere da a a f"
)
_A_KEY = "sembra una delle due chiavi, non un indirizzo: le chiavi vanno negli altri due secret"
_NO_ACCOUNT_IN_PAGE = "è una pagina del pannello di Cloudflare in cui non compare l'Account ID"


class StorageError(RuntimeError):
    """The bucket could not be read. The message is safe for a public log."""


class NotFound(StorageError):
    """Nothing is kept under that name."""


def _refused(why: str) -> StorageError:
    return StorageError(f"COSTS_R2_URL non valido: {why}. {EXPECTED}")


def _encoded(text: str, keep: str = "") -> str:
    """Percent-encoded the way the signature wants it: everything but letters, digits and ``-_.~``."""
    return quote(text, safe="-_.~" + keep)


@dataclass(frozen=True)
class Place:
    """Where an address points: a bucket, a folder in it (``key`` ends with "/") or one file."""

    host: str
    bucket: str
    key: str = ""

    @property
    def names_a_file(self) -> bool:
        return bool(self.key) and not self.key.endswith("/")

    @property
    def folder(self) -> str:
        """The folder pointed at, or the one the file is in: "" for the bucket itself, else ending in "/"."""
        if not self.names_a_file:
            return self.key
        head, slash, _ = self.key.rpartition("/")
        return head + slash


def _unwrapped(value: str) -> str:
    """A value as it may be pasted into a secret: with spaces around, in the quotes or brackets it was shown in."""
    text = _UNSEEN_RE.sub("", value).strip()
    for opening, closing in (('"', '"'), ("'", "'"), ("`", "`"), ("«", "»"), ("“", "”"), ("‘", "’"), ("<", ">")):
        if len(text) > 1 and text.startswith(opening) and text.endswith(closing):
            text = text[1:-1].strip()
    return text


def _host(account: str, jurisdiction: str = "") -> str:
    """The host of the S3 API for an account; a bucket kept in a jurisdiction ("eu") has its own."""
    return ".".join([account, *([jurisdiction] if jurisdiction else []), *_ENDPOINT])


def _from_dashboard(path: str, bucket: str | None) -> Place:
    """The bucket a page of Cloudflare's dashboard is about, read from the page's own address.

    That address is /<account>/r2/<jurisdiction>/buckets/<bucket>, then the tab
    being shown; "default" stands for no jurisdiction. It is what the browser's
    address bar holds while its owner looks at the bucket: the one thing about
    it that can be copied without composing anything.
    """
    parts = [part for part in path.split("/") if part]
    if not parts or not _ACCOUNT_RE.match(parts[0].lower()):
        raise _refused(_NO_ACCOUNT_IN_PAGE)
    jurisdiction, named = "", ""
    if "buckets" in parts[1:]:
        at = parts.index("buckets", 1)
        named = parts[at + 1] if at + 1 < len(parts) else ""
        if at == 3 and parts[1] == "r2" and parts[2].lower() != "default":
            jurisdiction = parts[2].lower()
            if not _LABEL_RE.match(jurisdiction):
                raise _refused(_NOT_THE_API)
    if not (named or bucket):
        raise _refused("è una pagina del pannello di Cloudflare, ma non quella di un bucket")
    return Place(_host(parts[0].lower(), jurisdiction), named or bucket)


def place(url: str, bucket: str | None = None) -> Place:
    """Where ``url`` points.

    Besides the address of the S3 API, with the bucket after the host or before
    it, what its owner has at hand is understood too: the Account ID alone, or
    the address of the bucket's page in Cloudflare's dashboard. ``bucket`` is
    the bucket meant when the address names only the account.

    Raises ``StorageError`` saying which part of the address is wrong. In words
    only: the address names the account, the bucket and the file, and the log
    this ends up in is public, so nothing of it is ever quoted.
    """
    text = _unwrapped(url)
    if _BLANK_RE.search(text):
        raise _refused(_PLACEHOLDER)
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise _refused("non è un indirizzo") from None
    labels = host.split(".")
    # "account-id" where the account goes; a bucket may well be called "bucket-foto".
    if not any(_ACCOUNT_RE.match(label) for label in labels) and _BLANK_HOST_RE.search(
        host.removesuffix("." + ".".join(_ENDPOINT))
    ):
        raise _refused(_PLACEHOLDER)
    if parts.scheme != "https":
        raise _refused("deve iniziare con https://")
    if parts.username or parts.password or port not in (None, 443):
        raise _refused("non deve contenere credenziali né una porta")
    path = unquote(parts.path).lstrip("/")
    if host == "dash.cloudflare.com":
        return _from_dashboard(path, bucket)  # whatever follows "?" there is the page's own business
    if host.endswith(".r2.dev"):
        raise _refused("è l'indirizzo pubblico r2.dev, serve quello dell'API S3")
    if host == "cloudflare.com" or host.endswith(".cloudflare.com"):
        raise _refused("è una pagina di Cloudflare, non l'indirizzo dell'API S3")
    named = ""
    if _ACCOUNT_RE.match(host):
        host = _host(host)  # the Account ID alone, as Cloudflare lets one copy it
    elif tuple(labels[-3:]) == _ENDPOINT:
        before = labels[:-3]
        if not before:
            raise _refused(_NO_ACCOUNT)
        if _ACCOUNT_RE.match(before[0]):
            account, after = before[0], before[1:]
        elif len(before) > 1 and _ACCOUNT_RE.match(before[1]):
            # "<bucket>.<account>..." names the bucket before the host: the same place as "<account>.../<bucket>".
            named, account, after = before[0], before[1], before[2:]
        else:
            raise _refused(_ODD_ACCOUNT)
        if len(after) > 1 or not all(_LABEL_RE.match(label) for label in after):
            raise _refused(_NOT_THE_API)
        host = _host(account, *after)
    elif _KEY_LIKE_RE.match(host):
        raise _refused(_A_KEY)
    else:
        raise _refused(_NOT_THE_API)
    if parts.query or parts.fragment:
        raise _refused("contiene «?» o «#»: niente parametri, e un nome di file senza quei segni")
    if named:
        return Place(host, named, path)
    named, _, key = path.partition("/")
    if not (named or bucket):
        raise _refused(_NO_BUCKET)
    return Place(host, named or bucket, key)


def canonical(url: str, bucket: str | None = None) -> str:
    """``url`` rewritten the one way the rest is given it: https://<host>/<bucket>, then the file if it names one.

    ``bucket`` is the bucket meant when ``url`` names only the account. Raises
    ``StorageError`` as ``place`` does.
    """
    where = place(url, bucket)
    return f"https://{where.host}/{_encoded(where.bucket)}" + (f"/{_encoded(where.key, keep='/')}" if where.key else "")


def sibling(url: str, name: str) -> str:
    """The address of the file called ``name`` in the folder ``url`` points to, or beside the file it names."""
    where = place(url)
    return f"https://{where.host}/{_encoded(where.bucket)}/{_encoded(where.folder, keep='/')}{_encoded(name)}"


# What R2 may call a refusal, in the body of its answer: words of the S3 protocol, never the
# owner's. Only these are ever repeated in a log.
_REFUSALS = {
    "SignatureDoesNotMatch": "firma rifiutata: la Secret Access Key non è quella di questo Access Key ID",
    "InvalidAccessKeyId": "Access Key ID non riconosciuto da Cloudflare: incollato male, oppure token eliminato",
    "Unauthorized": "chiavi non riconosciute da Cloudflare: incollate male, oppure token eliminato o scaduto",
    "AccessDenied": (
        "accesso negato: la chiave non può leggere questo bucket "
        "(token limitato a un altro bucket o senza permesso di lettura, oppure nome del bucket sbagliato)"
    ),
    "ExpiredToken": "chiave scaduta: va creato un nuovo token",
    "RequestTimeTooSkewed": "orologio del job non allineato a quello di Cloudflare",
}
_OTHER_CODES = frozenset({
    "AuthorizationHeaderMalformed", "InternalError", "InvalidArgument", "InvalidBucketName", "InvalidRequest",
    "InvalidURI", "MethodNotAllowed", "MissingSecurityHeader", "NotImplemented", "PermanentRedirect",
    "ServiceUnavailable", "SlowDown", "TooManyRequests",
})
_NO_SUCH_BUCKET = (
    "bucket non trovato: controlla il nome del bucket "
    "(per un bucket con giurisdizione europea l'host contiene «.eu.»)"
)
_CODE_RE = re.compile(rb"<Code>\s*([A-Za-z]{3,40})\s*</Code>")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _code(response: requests.Response) -> str:
    """What R2 calls its refusal ("AccessDenied", "NoSuchBucket"...), read from the start of its answer; "" if unsaid."""
    try:
        start = next(iter(response.iter_content(chunk_size=4096)), b"")
    except requests.RequestException:
        return ""
    found = _CODE_RE.search(start) if isinstance(start, bytes) else None
    return found.group(1).decode("ascii") if found else ""


def _keys_hint(key_id: str, secret: str) -> str:
    """What looks wrong in the two keys by their shape alone, for a refusal that may come from a slip in pasting them.

    A key pair of R2 is 32 and 64 hexadecimal characters: the id of the token
    and a digest of its value. Nothing of either key is repeated.
    """
    def looks(value: str, length: int) -> bool:
        return len(value) == length and bool(_HEX_RE.match(value))

    if looks(key_id, 64) and looks(secret, 32):
        return "le due chiavi sembrano scambiate tra i due secret"
    hints = []
    if not looks(key_id, 32):
        hints.append("COSTS_R2_ACCESS_KEY_ID non ha la forma di un Access Key ID di R2 (32 caratteri esadecimali)")
    if not looks(secret, 64):
        hints.append(
            "COSTS_R2_SECRET_ACCESS_KEY non ha la forma di una Secret Access Key di R2 "
            "(64 caratteri esadecimali): non è il «Token value»"
        )
    return "; ".join(hints)


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def canonical_query(parameters: Mapping[str, str]) -> str:
    """The query string as the signature wants it, and as it is then sent: sorted, every value encoded."""
    return "&".join(f"{_encoded(name)}={_encoded(value)}" for name, value in sorted(parameters.items()))


def authorization(
    method: str,
    host: str,
    path: str,
    headers: Mapping[str, str],
    *,
    key_id: str,
    secret: str,
    when: datetime,
    query: str = "",
    region: str = REGION,
    service: str = SERVICE,
    payload_hash: str = EMPTY_SHA256,
) -> str:
    """The ``Authorization`` header of a Signature Version 4 request.

    ``path`` is already percent-encoded and ``query`` already canonical (see
    ``canonical_query``); ``headers`` are the ones to sign besides ``host``,
    ``x-amz-content-sha256`` and ``x-amz-date``, which are always signed and
    must be sent with the values used here.
    """
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    signed = {name.lower(): " ".join(str(value).split()) for name, value in headers.items()}
    signed.update({"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": stamp})
    names = sorted(signed)
    request = "\n".join([
        method,
        path,
        query,
        "".join(f"{name}:{signed[name]}\n" for name in names),
        ";".join(names),
        payload_hash,
    ])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(request.encode("utf-8")).hexdigest()])
    key = ("AWS4" + secret).encode("utf-8")
    for part in (day, region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"


def _get(
    host: str,
    path: str,
    query: str,
    key_id: str,
    secret: str,
    *,
    session: requests.Session | None,
    now: datetime | None,
    max_bytes: int,
    missing: str,
) -> bytes:
    """The body of a signed GET. Raises ``StorageError``; ``NotFound`` (saying ``missing``) on a 404."""
    when = now or datetime.now(timezone.utc)
    key_id, secret = _unwrapped(key_id), _unwrapped(secret)
    headers = {
        "x-amz-content-sha256": EMPTY_SHA256,
        "x-amz-date": when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    }
    headers["Authorization"] = authorization("GET", host, path, {}, key_id=key_id, secret=secret, when=when, query=query)
    try:
        # No redirects: the signature is for this host, and nothing else should see the request.
        with (session or requests.Session()).get(
            f"https://{host}{path}" + (f"?{query}" if query else ""),
            headers=headers,
            timeout=TIMEOUT_SECONDS,
            stream=True,
            allow_redirects=False,
        ) as response:
            status = response.status_code
            code = _code(response) if status != 200 else ""
            if status in (401, 403):
                reason = _REFUSALS.get(code, "chiave rifiutata, oppure senza il permesso di lettura su questo bucket")
                hint = _keys_hint(key_id, secret)
                raise StorageError(reason + (f". Nota: {hint}" if hint else ""))
            if status == 404:
                if code == "NoSuchBucket":
                    raise StorageError(_NO_SUCH_BUCKET)  # not a file that is missing: nothing can be there
                raise NotFound(missing)
            if status != 200:
                known = code in _REFUSALS or code in _OTHER_CODES or code.startswith("NoSuch")
                raise StorageError(f"HTTP {status}" + (f" ({code})" if known else ""))
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise StorageError(f"file troppo grande (oltre {max_bytes // (1024 * 1024)} MB)")
            return bytes(body)
    except requests.RequestException as exc:
        raise StorageError(f"Cloudflare R2 non raggiungibile ({type(exc).__name__})") from None


def fetch(
    url: str,
    key_id: str,
    secret: str,
    *,
    session: requests.Session | None = None,
    now: datetime | None = None,
    max_bytes: int = MAX_BYTES,
) -> bytes:
    """The bytes of the file at ``url``.

    Raises ``StorageError`` with a message fit for a public log; ``NotFound``
    when nothing is kept under that name.
    """
    where = place(url)
    if not where.names_a_file:
        raise _refused(_NO_FILE)
    return _get(
        where.host,
        f"/{_encoded(where.bucket)}/{_encoded(where.key, keep='/')}",
        "",
        key_id,
        secret,
        session=session,
        now=now,
        max_bytes=max_bytes,
        missing="file non trovato: controlla il nome del bucket e del file in COSTS_R2_URL",
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class Entry:
    """A file as a listing shows it: its name in the folder, its size, when it was last written."""

    name: str
    size: int = -1  # in bytes; -1 where the listing does not say
    modified: str = ""  # as R2 writes it (2026-11-22T08:00:00.000Z): in order of time when sorted as text


def names(
    url: str,
    key_id: str,
    secret: str,
    *,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[str]:
    """The names of the files in the folder ``url`` points to (the folder of the file, if it names one)."""
    return [entry.name for entry in entries(url, key_id, secret, session=session, now=now)]


def entries(
    url: str,
    key_id: str,
    secret: str,
    *,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[Entry]:
    """The files in the folder ``url`` points to (the folder of the file, if it names one).

    Only the files directly there, not those of folders inside it. Raises
    ``StorageError`` with a message fit for a public log.
    """
    where = place(url)
    parameters = {"delimiter": "/", "list-type": "2", "max-keys": str(LISTING_MAX_NAMES)}
    if where.folder:
        parameters["prefix"] = where.folder
    body = _get(
        where.host,
        f"/{_encoded(where.bucket)}",
        canonical_query(parameters),
        key_id,
        secret,
        session=session,
        now=now,
        max_bytes=LISTING_MAX_BYTES,
        missing=_NO_SUCH_BUCKET,
    )
    unrecognised = StorageError("risposta di Cloudflare R2 non riconosciuta")
    if b"<!" in body:  # a listing declares no document type and no entity: nothing to expand
        raise unrecognised
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise unrecognised from None
    if _local(root.tag) != "ListBucketResult":
        raise unrecognised
    if any(_local(child.tag) == "IsTruncated" and (child.text or "").strip().lower() == "true" for child in root):
        raise StorageError("nel bucket ci sono troppi file per trovare da solo quello giusto: indica il file in COSTS_R2_URL")
    found = []
    for child in root:
        if _local(child.tag) != "Contents":
            continue
        said = {_local(part.tag): (part.text or "") for part in child}
        key, size = said.get("Key", ""), said.get("Size", "").strip()
        if not key.startswith(where.folder):
            continue
        name = key[len(where.folder):]
        if name and "/" not in name:
            found.append(Entry(name, int(size) if size.isdigit() else -1, said.get("LastModified", "").strip()))
    return found
