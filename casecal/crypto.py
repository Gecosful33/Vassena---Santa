"""Two-tier envelope encryption for a page served from a public host.

The published HTML only ever contains ciphertext. Two passphrases unlock it:

* the *family* passphrase decrypts the availability calendar;
* the *owner* passphrase decrypts the economics and, because the owner blob
  carries the family key, the calendar as well.

Keys come from PBKDF2-HMAC-SHA256 and blobs are AES-256-GCM, both of which the
browser reproduces natively through WebCrypto (see ``web/crypto.js``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import unicodedata
from typing import Any, Iterable, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ENVELOPE_VERSION = 1
KDF_ITERATIONS = 600_000
KEY_BYTES = 32
SALT_BYTES = 16
IV_BYTES = 12
# Plaintext is padded to this block so that the ciphertext length, which anyone
# can read, does not reveal how many bookings were added overnight.
PAD_BLOCK = 8192
MIN_PASSPHRASE_LENGTH = 16
MIN_DISTINCT_CHARACTERS = 6

AAD_FAMILY = "casecal/v1/family"
AAD_OWNER = "casecal/v1/owner"

_PASSPHRASE_ALPHABET = "bcdfghjkmnpqrstvwxz"


class PassphraseError(ValueError):
    """A passphrase is too weak to protect ciphertext that is public."""


class EnvelopeError(ValueError):
    """The envelope is malformed or was produced by an incompatible version."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def normalize_passphrase(passphrase: str) -> str:
    """Canonical form shared with the browser: NFKC, surrounding blanks removed."""
    return unicodedata.normalize("NFKC", passphrase).strip()


def site_salt(site_id: str) -> bytes:
    """Deterministic per-site salt.

    It must stay stable across nightly rebuilds, otherwise the key a device
    remembered yesterday would stop working today.
    """
    return hashlib.sha256(b"casecal/v1/salt\x00" + site_id.encode("utf-8")).digest()[:SALT_BYTES]


def derive_key(passphrase: str, salt: bytes, iterations: int = KDF_ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, iterations=iterations)
    return kdf.derive(normalize_passphrase(passphrase).encode("utf-8"))


def encrypt_json(key: bytes, payload: Any, *, aad: str) -> dict[str, str]:
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padded = plaintext + b" " * (-len(plaintext) % PAD_BLOCK)
    iv = os.urandom(IV_BYTES)
    ciphertext = AESGCM(key).encrypt(iv, padded, aad.encode("ascii"))
    return {"iv": _b64(iv), "ct": _b64(ciphertext)}


def decrypt_json(key: bytes, blob: Mapping[str, str], *, aad: str) -> Any:
    """Return the decrypted payload, or raise ``InvalidTag`` on a wrong key."""
    plaintext = AESGCM(key).decrypt(_unb64(blob["iv"]), _unb64(blob["ct"]), aad.encode("ascii"))
    return json.loads(plaintext.decode("utf-8"))


def check_passphrases(family: str, owner: str, *, forbidden: Iterable[str] = ()) -> None:
    """Refuse passphrases an offline dictionary attack would find.

    The ciphertext is public, so an attacker can test guesses offline at the
    speed of their hardware: length is the only real defence.
    """
    banned = {normalize_passphrase(item).casefold() for item in forbidden if item}
    normalised = {}
    for name, value in (("FAMILY_PASSPHRASE", family), ("OWNER_PASSPHRASE", owner)):
        text = normalize_passphrase(value or "")
        if len(text) < MIN_PASSPHRASE_LENGTH:
            raise PassphraseError(
                f"{name}: servono almeno {MIN_PASSPHRASE_LENGTH} caratteri "
                "(generane una con `python -m casecal passphrase`)"
            )
        if len(set(text)) < MIN_DISTINCT_CHARACTERS:
            raise PassphraseError(f"{name}: troppo ripetitiva, usa caratteri più vari")
        folded = text.casefold()
        if any(item and (item == folded or (len(item) >= 6 and item in folded)) for item in banned):
            raise PassphraseError(f"{name}: non può coincidere con le credenziali dei portali")
        normalised[name] = text
    if normalised["FAMILY_PASSPHRASE"] == normalised["OWNER_PASSPHRASE"]:
        raise PassphraseError("FAMILY_PASSPHRASE e OWNER_PASSPHRASE devono essere diverse")


def generate_passphrase(groups: int = 5, group_length: int = 4) -> str:
    """Random passphrase that is easy to type on a phone (about 85 bits)."""
    return "-".join(
        "".join(secrets.choice(_PASSPHRASE_ALPHABET) for _ in range(group_length))
        for _ in range(groups)
    )


def build_envelope(
    *,
    family_passphrase: str,
    owner_passphrase: str,
    salt: bytes,
    family_payload: Mapping[str, Any],
    owner_payload: Mapping[str, Any],
    iterations: int = KDF_ITERATIONS,
) -> dict[str, Any]:
    family_key = derive_key(family_passphrase, salt, iterations)
    owner_key = derive_key(owner_passphrase, salt, iterations)
    owner_plain = dict(owner_payload)
    owner_plain["family_key"] = _b64(family_key)
    return {
        "v": ENVELOPE_VERSION,
        "kdf": {
            "name": "PBKDF2",
            "hash": "SHA-256",
            "iterations": iterations,
            "salt": _b64(salt),
        },
        "family": encrypt_json(family_key, family_payload, aad=AAD_FAMILY),
        "owner": encrypt_json(owner_key, owner_plain, aad=AAD_OWNER),
    }


def open_envelope(
    envelope: Mapping[str, Any],
    *,
    family_passphrase: str | None = None,
    owner_passphrase: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Decrypt whatever the given passphrases unlock.

    Returns ``(family_payload, owner_payload)``; a tier whose passphrase is
    missing or wrong comes back as ``None``.
    """
    try:
        if envelope["v"] != ENVELOPE_VERSION:
            raise EnvelopeError("unsupported envelope version")
        kdf = envelope["kdf"]
        salt = _unb64(kdf["salt"])
        iterations = int(kdf["iterations"])
        family_blob, owner_blob = envelope["family"], envelope["owner"]
    except (KeyError, TypeError, ValueError) as exc:
        raise EnvelopeError("malformed envelope") from exc
    if kdf.get("name") != "PBKDF2" or kdf.get("hash") != "SHA-256":
        raise EnvelopeError("unsupported key derivation")
    if not 100_000 <= iterations <= 5_000_000:
        raise EnvelopeError("implausible iteration count")

    family: dict[str, Any] | None = None
    owner: dict[str, Any] | None = None
    if owner_passphrase:
        try:
            owner = decrypt_json(
                derive_key(owner_passphrase, salt, iterations), owner_blob, aad=AAD_OWNER
            )
        except InvalidTag:
            owner = None
    if family_passphrase:
        try:
            family = decrypt_json(
                derive_key(family_passphrase, salt, iterations), family_blob, aad=AAD_FAMILY
            )
        except InvalidTag:
            family = None
    if family is None and owner is not None:
        try:
            family = decrypt_json(_unb64(owner["family_key"]), family_blob, aad=AAD_FAMILY)
        except (InvalidTag, KeyError, ValueError):
            family = None
    return family, owner
