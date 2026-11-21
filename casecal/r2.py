"""Read one object from a private Cloudflare R2 bucket.

R2 speaks the S3 protocol: a GET signed with AWS Signature Version 4, with a key
the owner creates for this purpose alone (permission "Object Read only", limited
to the bucket). Nothing is ever written there.

The signing is done here, in a few lines, rather than through an SDK: one
request of one kind does not justify a dependency of that size in an unattended
job. It is held to the worked example Amazon publishes (see the tests).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import quote, unquote, urlsplit

import requests

REGION, SERVICE = "auto", "s3"  # R2 has one region, called "auto"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 30

_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.r2\.cloudflarestorage\.com$")
URL_SHAPE = "https://<account>.r2.cloudflarestorage.com/<bucket>/<file>"


class StorageError(RuntimeError):
    """The object could not be read. The message is safe for a public log."""


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def authorization(
    method: str,
    host: str,
    path: str,
    headers: Mapping[str, str],
    *,
    key_id: str,
    secret: str,
    when: datetime,
    region: str = REGION,
    service: str = SERVICE,
    payload_hash: str = EMPTY_SHA256,
) -> str:
    """The ``Authorization`` header of a Signature Version 4 request without a query string.

    ``path`` is already percent-encoded; ``headers`` are the ones to sign besides
    ``host``, ``x-amz-content-sha256`` and ``x-amz-date``, which are always signed
    and must be sent with the values used here.
    """
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    signed = {name.lower(): " ".join(str(value).split()) for name, value in headers.items()}
    signed.update({"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": stamp})
    names = sorted(signed)
    request = "\n".join([
        method,
        path,
        "",  # no query string
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


def _target(url: str) -> tuple[str, str]:
    """Host and percent-encoded path of the object, or ``StorageError`` if the address is not one."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise StorageError(f"COSTS_R2_URL non valido: deve avere la forma {URL_SHAPE}") from None
    path = quote(unquote(parts.path), safe="/-_.~")
    bucket, _, name = path.lstrip("/").partition("/")
    if (
        parts.scheme != "https"
        or not _HOST_RE.match(host)
        or port not in (None, 443)
        or parts.query
        or parts.fragment
        or parts.username
        or not bucket
        or not name
        or name.endswith("/")
    ):
        raise StorageError(f"COSTS_R2_URL non valido: deve avere la forma {URL_SHAPE}")
    return host, path


def fetch(
    url: str,
    key_id: str,
    secret: str,
    *,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> bytes:
    """The bytes of the object at ``url``. Raises ``StorageError`` with a message fit for a public log."""
    host, path = _target(url)
    when = now or datetime.now(timezone.utc)
    headers = {
        "x-amz-content-sha256": EMPTY_SHA256,
        "x-amz-date": when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    }
    headers["Authorization"] = authorization(
        "GET", host, path, {}, key_id=key_id.strip(), secret=secret.strip(), when=when
    )
    try:
        # No redirects: the signature is for this host, and nothing else should see the request.
        with (session or requests.Session()).get(
            f"https://{host}{path}",
            headers=headers,
            timeout=TIMEOUT_SECONDS,
            stream=True,
            allow_redirects=False,
        ) as response:
            status = response.status_code
            if status in (401, 403):
                raise StorageError("chiave rifiutata, oppure senza il permesso di lettura su questo bucket")
            if status == 404:
                raise StorageError("file non trovato: controlla il nome del bucket e del file in COSTS_R2_URL")
            if status != 200:
                raise StorageError(f"HTTP {status}")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise StorageError(f"file troppo grande (oltre {MAX_BYTES // (1024 * 1024)} MB)")
            return bytes(body)
    except requests.RequestException as exc:
        raise StorageError(f"Cloudflare R2 non raggiungibile ({type(exc).__name__})") from None
