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

_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.r2\.cloudflarestorage\.com$")
_ACCOUNT_RE = re.compile(r"^[0-9a-f]{32}$")  # what Cloudflare calls the Account ID
# What the address must look like, as its owner finds it: Cloudflare shows it, ready to be
# copied, among the settings of the bucket.
EXPECTED = (
    "Forma attesa: l'indirizzo «S3 API» del bucket, "
    "https://<account>.r2.cloudflarestorage.com/<bucket>, seguito se serve da /<file>"
)
_NO_FILE = "dopo il nome del bucket manca il nome del file"


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


def place(url: str) -> Place:
    """Where ``url`` points.

    Raises ``StorageError`` saying which part of the address is wrong. In words
    only: the address names the account, the bucket and the file, and the log
    this ends up in is public, so nothing of it is ever quoted.
    """
    # As it may be pasted into a secret: in quotes or brackets, or without its scheme.
    text = url.strip().strip("\"'<>«»").strip()
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise _refused("non è un indirizzo") from None
    if parts.scheme != "https":
        raise _refused("deve iniziare con https://")
    if not _HOST_RE.match(host):
        if host.endswith(".r2.dev"):
            raise _refused("è l'indirizzo pubblico r2.dev, serve quello dell'API S3")
        if host.endswith("cloudflare.com"):
            raise _refused("è una pagina del pannello di Cloudflare, serve l'indirizzo dell'API S3")
        raise _refused("l'host non è <account>.r2.cloudflarestorage.com")
    if parts.username or parts.password or port not in (None, 443):
        raise _refused("non deve contenere credenziali né una porta")
    if parts.query or parts.fragment:
        raise _refused("contiene «?» o «#»: niente parametri, e un nome di file senza quei segni")
    path = unquote(parts.path).lstrip("/")
    # "<bucket>.<account>..." names the bucket before the host: the same place as "<account>.../<bucket>".
    labels = host.split(".")
    if len(labels) > 4 and _ACCOUNT_RE.match(labels[1]):
        return Place(".".join(labels[1:]), labels[0], path)
    bucket, _, key = path.partition("/")
    if not bucket:
        raise _refused("dopo l'host manca il nome del bucket")
    return Place(host, bucket, key)


def sibling(url: str, name: str) -> str:
    """The address of the file called ``name`` in the folder ``url`` points to, or beside the file it names."""
    where = place(url)
    return f"https://{where.host}/{_encoded(where.bucket)}/{_encoded(where.folder, keep='/')}{_encoded(name)}"


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
    headers = {
        "x-amz-content-sha256": EMPTY_SHA256,
        "x-amz-date": when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    }
    headers["Authorization"] = authorization(
        "GET", host, path, {}, key_id=key_id.strip(), secret=secret.strip(), when=when, query=query
    )
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
            if status in (401, 403):
                raise StorageError("chiave rifiutata, oppure senza il permesso di lettura su questo bucket")
            if status == 404:
                raise NotFound(missing)
            if status != 200:
                raise StorageError(f"HTTP {status}")
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


def names(
    url: str,
    key_id: str,
    secret: str,
    *,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[str]:
    """The names of the files in the folder ``url`` points to (the folder of the file, if it names one).

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
        missing=(
            "bucket non trovato: controlla il nome del bucket in COSTS_R2_URL "
            "(per un bucket con giurisdizione europea l'host contiene «.eu.»)"
        ),
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
    keys = [
        key.text or ""
        for child in root
        if _local(child.tag) == "Contents"
        for key in child
        if _local(key.tag) == "Key"
    ]
    here = [key[len(where.folder):] for key in keys if key.startswith(where.folder)]
    return [name for name in here if name and "/" not in name]
