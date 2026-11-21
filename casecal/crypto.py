"""Envelope encryption for a page served from a public host.

The published HTML only ever contains ciphertext. One passphrase unlocks it:
whoever has it sees the calendar and the figures alike.

The page carries two blobs, the calendar ("family") and the figures ("owner"),
under the same key and told apart by their associated data. They used to open
with two different passphrases; ``open_envelope`` can still read a page
published that way, which is what the nightly job finds online the first time
it runs after the change.

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


def encrypt_json(key: bytes, payload: Any, *, aad: str, pad: int = PAD_BLOCK) -> dict[str, str]:
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padded = plaintext + b" " * (-len(plaintext) % pad)
    iv = os.urandom(IV_BYTES)
    ciphertext = AESGCM(key).encrypt(iv, padded, aad.encode("ascii"))
    return {"iv": _b64(iv), "ct": _b64(ciphertext)}


def decrypt_json(key: bytes, blob: Mapping[str, str], *, aad: str) -> Any:
    """Return the decrypted payload, or raise ``InvalidTag`` on a wrong key."""
    plaintext = AESGCM(key).decrypt(_unb64(blob["iv"]), _unb64(blob["ct"]), aad.encode("ascii"))
    return json.loads(plaintext.decode("utf-8"))


def check_passphrase(passphrase: str, *, forbidden: Iterable[str] = (), name: str = "FAMILY_PASSPHRASE") -> None:
    """Refuse a passphrase an offline dictionary attack would find.

    The ciphertext is public, so an attacker can test guesses offline at the
    speed of their hardware: length is the only real defence.
    """
    banned = {normalize_passphrase(item).casefold() for item in forbidden if item}
    text = normalize_passphrase(passphrase or "")
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


def generate_passphrase(groups: int = 5, group_length: int = 4) -> str:
    """Random passphrase that is easy to type on a phone (about 85 bits)."""
    return "-".join(
        "".join(secrets.choice(_PASSPHRASE_ALPHABET) for _ in range(group_length))
        for _ in range(groups)
    )


def build_envelope(
    *,
    passphrase: str,
    salt: bytes,
    family_payload: Mapping[str, Any],
    owner_payload: Mapping[str, Any],
    iterations: int = KDF_ITERATIONS,
) -> dict[str, Any]:
    key = derive_key(passphrase, salt, iterations)
    return {
        "v": ENVELOPE_VERSION,
        "kdf": {
            "name": "PBKDF2",
            "hash": "SHA-256",
            "iterations": iterations,
            "salt": _b64(salt),
        },
        "family": encrypt_json(key, family_payload, aad=AAD_FAMILY),
        "owner": encrypt_json(key, owner_payload, aad=AAD_OWNER),
    }


def open_envelope(
    envelope: Mapping[str, Any],
    *,
    passphrase: str,
    legacy_owner_passphrase: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Decrypt what the passphrase unlocks.

    Returns ``(family_payload, owner_payload)``; a blob the passphrase does not
    open comes back as ``None``. ``legacy_owner_passphrase`` is tried on the
    figures of a page published when they had a passphrase of their own.
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

    def opened(key: bytes, blob: Mapping[str, str], aad: str) -> dict[str, Any] | None:
        try:
            return decrypt_json(key, blob, aad=aad)
        except InvalidTag:
            return None

    family = owner = None
    if passphrase:
        key = derive_key(passphrase, salt, iterations)  # derived once: it is the slow part
        family = opened(key, family_blob, AAD_FAMILY)
        owner = opened(key, owner_blob, AAD_OWNER)
    if owner is None and legacy_owner_passphrase:
        owner = opened(derive_key(legacy_owner_passphrase, salt, iterations), owner_blob, AAD_OWNER)
    return family, owner
